import builtins as _builtins
import os
from typing import Optional
from pathlib import Path
from datetime import datetime, date
from collections import deque
from threading import Lock
import traceback
import threading
import time

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ai_router import ask_ai
from reminder_manager import add_reminder, list_reminders, delete_reminder, due_reminders, recent_tasks
from notes import (
    add_note,
    get_pending_notes,
    get_all_notes,
    complete_note,
    reopen_note,
    delete_note_by_id,
)
from memory import list_memories, remember, forget_memory
import ai_router as _router
import mirror_link


def print(*args, **kwargs):  # NFR-SEC3: no chat text in the system log
    if os.environ.get("VISION_DEBUG") == "1":
        _builtins.print(*args, **kwargs)

from voice_test import (
    start_voice_service,
    stop_voice_service,
    get_voice_status,
)

BASE_DIR = Path(__file__).resolve().parent
DASHBOARD_FILE = BASE_DIR / "visionsync_chatbot_interface.html"

app = FastAPI(title="VisionSync AI", version="1.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ChatRequest(BaseModel):
    message: str

class ReminderCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    due_at: str

class TaskCreate(BaseModel):
    note: str = Field(min_length=1, max_length=500)
    note_date: Optional[str] = None

class MemoryCreate(BaseModel):
    information: str = Field(min_length=1, max_length=1000)
    category: str = Field(default="personal", max_length=80)
    key: Optional[str] = Field(default=None, max_length=120)

_voice_events = deque(maxlen=300)
_event_lock = Lock()

def publish_voice_event(event):
    with _event_lock:
        _voice_events.append({
            **event,
            "timestamp": datetime.now().isoformat()
        })


@app.on_event("startup")
def start_voice_automatically():
    """Start the local microphone/wake-word service when VisionSync starts."""
    try:
        result = (start_voice_service(publish_voice_event) if os.environ.get("VISION_VOICE_AUTOSTART", "1") != "0" else {"message": "voice autostart is off (VISION_VOICE_AUTOSTART=0)"})
        print("[VisionSync] Hands-free wake-word service:", result.get("message", result))
    except Exception as error:
        # Keep the dashboard/API available even if the microphone is unavailable.
        print("[VisionSync] Could not auto-start voice service:", repr(error))

def _task_dict(task, include_status=False):
    # pending rows: (id, note, created_at)
    if include_status:
        task_id, note, task_date, created_at, status = task
        return {
            "id": task_id,
            "note": note,
            "date": task_date,
            "created_at": created_at,
            "status": status,
        }

    task_id, note, created_at = task
    return {
        "id": task_id,
        "note": note,
        "created_at": created_at,
        "date": date.today().isoformat(),
        "status": "pending",
    }

@app.get("/")
def home():
    return {
        "application": "VisionSync",
        "status": "online",
        "dashboard": "/dashboard"
    }

@app.get("/dashboard")
def dashboard():
    if not DASHBOARD_FILE.exists():
        raise HTTPException(
            status_code=404,
            detail="VisionSync dashboard HTML not found."
        )
    return FileResponse(DASHBOARD_FILE, media_type="text/html")

@app.get("/api/health")
def health():
    return {
        "status": "online",
        "assistant": "Vision",
        "timestamp": datetime.now().isoformat()
    }

# -------------------- MEMORY API --------------------

@app.get("/api/memories")
def memory_list():
    """Return saved memories; database IDs are retained only for internal actions."""
    try:
        return {"success": True, "memories": list_memories(200)}
    except Exception as error:
        print("[MEMORY LIST ERROR]", repr(error))
        raise HTTPException(status_code=500, detail="Could not load memories.")

@app.post("/api/memories")
def memory_add(request: MemoryCreate):
    try:
        memory_id = remember(
            request.information.strip(),
            category=request.category.strip() or "personal",
            key=request.key.strip() if request.key else None,
        )
        row = next((item for item in list_memories(200) if int(item["id"]) == int(memory_id)), None)
        if row is None:
            raise RuntimeError("Saved memory could not be verified.")
        return {"success": True, "memory": row}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    except Exception as error:
        print("[MEMORY SAVE ERROR]", repr(error))
        raise HTTPException(status_code=500, detail="Could not save memory.")

@app.delete("/api/memories/{memory_id}")
def memory_remove(memory_id: int):
    try:
        if not forget_memory(memory_id):
            raise HTTPException(status_code=404, detail="Memory not found.")
        return {"success": True, "deleted": True}
    except HTTPException:
        raise
    except Exception as error:
        print("[MEMORY DELETE ERROR]", repr(error))
        raise HTTPException(status_code=500, detail="Could not delete memory.")

@app.post("/api/chat")
def chat(request: ChatRequest):
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    try:
        print(f"\n[API received]: {message}")
        response = ask_ai(message)
        mirror_link.report_mode(_router.LAST_ROUTE)
        if not response:
            raise RuntimeError("AI returned an empty response.")
        print(f"[Vision replied]: {response}")
        return {"success": True, "response": response, "mode": _router.LAST_ROUTE}
    except Exception as error:
        print("[API ERROR]", repr(error))
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail="Vision encountered an internal error."
        )

# -------------------- TASK API --------------------

def _open_tasks():
    """Pending tasks of the last seven days and of any later day (the phone app's list)."""
    import sqlite3 as _sqlite
    from datetime import timedelta as _td
    import reminder_manager as _rm
    first = (date.today() - _td(days=6)).isoformat()
    conn = _sqlite.connect(str(_rm.NOTES_DB), timeout=15)
    conn.row_factory = _sqlite.Row
    try:
        rows = conn.execute("SELECT id, note, note_date, created_at, status FROM notes WHERE status = 'pending' "
                            "AND note_date >= ? ORDER BY note_date, id LIMIT 200", (first,)).fetchall()
    except _sqlite.OperationalError:
        rows = []
    finally:
        conn.close()
    return [dict(r) for r in rows]


@app.get("/api/tasks")
def list_tasks(view: str = Query(default="pending", pattern="^(pending|all|open)$")):
    try:
        # Seven calendar days: today plus previous six days ("open": the days to come too, for the app).
        tasks = _open_tasks() if view == "open" else recent_tasks(days=7, pending_only=(view == "pending"))

        return {
            "success": True,
            "view": view,
            "count": len(tasks),
            "tasks": tasks,
        }
    except Exception as error:
        print("[TASK LIST ERROR]", repr(error))
        raise HTTPException(status_code=500, detail="Could not load tasks.")

@app.post("/api/tasks")
def create_task(request: TaskCreate):
    note = request.note.strip()
    if not note:
        raise HTTPException(status_code=400, detail="Task cannot be empty.")

    try:
        task_id = add_note(note, request.note_date)
        return {
            "success": True,
            "message": "Task saved.",
            "task": {
                "id": task_id,
                "note": note,
                "date": request.note_date or date.today().isoformat(),
                "status": "pending",
            }
        }
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    except Exception as error:
        print("[TASK CREATE ERROR]", repr(error))
        raise HTTPException(status_code=500, detail="Could not save task.")

@app.patch("/api/tasks/{task_id}/complete")
def mark_task_complete(task_id: int):
    try:
        if not complete_note(task_id):
            raise HTTPException(
                status_code=404,
                detail="Task not found or already completed."
            )
        return {"success": True, "message": "Task marked completed.", "id": task_id}
    except HTTPException:
        raise
    except Exception as error:
        print("[TASK COMPLETE ERROR]", repr(error))
        raise HTTPException(status_code=500, detail="Could not complete task.")

@app.patch("/api/tasks/{task_id}/reopen")
def reopen_task(task_id: int):
    try:
        if not reopen_note(task_id):
            raise HTTPException(
                status_code=404,
                detail="Task not found or already pending."
            )
        return {"success": True, "message": "Task moved back to pending.", "id": task_id}
    except HTTPException:
        raise
    except Exception as error:
        print("[TASK REOPEN ERROR]", repr(error))
        raise HTTPException(status_code=500, detail="Could not reopen task.")

@app.delete("/api/tasks/{task_id}")
def remove_task(task_id: int):
    try:
        if not delete_note_by_id(task_id):
            raise HTTPException(status_code=404, detail="Task not found.")
        return {"success": True, "message": "Task deleted.", "id": task_id}
    except HTTPException:
        raise
    except Exception as error:
        print("[TASK DELETE ERROR]", repr(error))
        raise HTTPException(status_code=500, detail="Could not delete task.")

@app.post("/api/tasks/{task_id}/complete")
def task_complete(task_id: int):
    """Marks a mirror task done (the checkbox in the phone app)."""
    import tools as _tools
    result = _tools.complete_task(task_id)
    if isinstance(result, dict) and result.get("success") is False:
        raise HTTPException(status_code=404, detail="Task not found.")
    return {"success": True, "id": task_id}


# -------------------- VOICE API --------------------

@app.post("/api/voice/start")
def voice_start():
    result = start_voice_service(publish_voice_event)
    return {**result, "voice": get_voice_status()}

@app.post("/api/voice/stop")
def voice_stop():
    result = stop_voice_service()
    return {**result, "voice": get_voice_status()}

@app.get("/api/voice/status")
def voice_status():
    return get_voice_status()

@app.get("/api/voice/events")
def voice_events():
    with _event_lock:
        events = list(_voice_events)
        _voice_events.clear()
    return {"events": events}


@app.get("/api/tasks/archive")
def archived_tasks():
    """Recover older tasks without keeping them in the mirror's seven-day view."""
    from datetime import timedelta
    cutoff=(date.today()-timedelta(days=6)).isoformat()
    rows=get_all_notes()
    archived=[_task_dict(row, include_status=True) for row in rows if row[2]<cutoff]
    return {"success":True,"count":len(archived),"tasks":archived}

# -------------------- PHASE 5: REMINDERS --------------------
@app.get("/api/reminders")
def reminder_list():
    return {"success": True, "reminders": list_reminders()}

@app.post("/api/reminders")
def reminder_add(request: ReminderCreate):
    try:
        return {"success": True, "reminder": add_reminder(request.title, request.due_at)}
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.delete("/api/reminders/{reminder_id}")
def reminder_remove(reminder_id: int):
    if not delete_reminder(reminder_id):
        raise HTTPException(status_code=404, detail="Reminder not found.")
    return {"success": True, "id": reminder_id}

@app.post("/api/reminders/{reminder_id}/done")
def reminder_done(reminder_id: int):
    """Ticked in the phone app: Vision stops showing and saying it. Its Google Calendar event goes
    too if Vision made it; an event that came from Google Calendar stays there."""
    import sqlite3 as _sqlite
    import reminder_manager as _rm
    conn = _sqlite.connect(str(_rm.REMINDER_DB), timeout=15)
    try:
        with conn:
            changed = conn.execute("UPDATE reminders SET status = 'done' WHERE id = ?", (reminder_id,)).rowcount
    finally:
        conn.close()
    if not changed:
        raise HTTPException(status_code=404, detail="Reminder not found.")
    _kick_google()
    return {"success": True, "id": reminder_id}


class GoogleLinkRequest(BaseModel):
    refresh_token: str = Field(min_length=10, max_length=4096)
    access_token: Optional[str] = Field(default=None, max_length=4096)


@app.post("/api/google/link")
def google_link_from_app(request: GoogleLinkRequest):
    """The Google account connected in the phone app becomes the mirror's (its tasks and calendar)."""
    import google_link as _gl
    import google_sync
    try:
        who = google_sync.link(request.refresh_token, request.access_token)
    except _gl.GoogleError as exc:
        return {"success": False, "problem": str(exc)}
    return {"success": True, "email": who.get("email", ""), "problem": who.get("problem", "")}


@app.post("/api/google/unlink")
def google_unlink_from_app():
    """"Disconnect" in the app: the mirror stops syncing that account and takes its items off."""
    import google_sync
    google_sync.unlink()
    return {"success": True}


@app.post("/api/google/token")
def google_token_for_app():
    """A fresh Google access token for the phone app, so it never has to open the Google page."""
    import google_link as _gl
    try:
        return dict(_gl.app_token(), success=True)
    except _gl.GoogleError as exc:
        return {"success": False, "problem": str(exc)}


@app.get("/api/google")
def google_status():
    """Which Google account the mirror syncs, any problem, and which tasks and reminders are on Google."""
    import google_sync
    return google_sync.status()


@app.post("/api/google/sync")
def google_sync_now():
    """The phone app changed Google: read it now instead of in 30 seconds."""
    _kick_google()
    return {"success": True}


def _kick_google():
    try:
        import google_sync
        google_sync.kick()
    except Exception:
        pass


_reminder_stop = threading.Event()
_reminder_thread = None

def _push_agenda():
    """Pending tasks and upcoming reminders, shown on the mirror dashboard."""
    def field(row, *names):
        for n in names:
            try:
                value = row[n]
            except (KeyError, IndexError, TypeError):
                continue
            if value:
                return value
        return ""
    try:
        rows = recent_tasks(days=7, pending_only=True)
    except TypeError:
        rows = recent_tasks()
    tasks = [str(field(t, "note", "title", "text")) for t in rows][:8]
    reminders = [{"title": str(field(r, "title")), "due_at": str(field(r, "due_at"))} for r in list_reminders()[:6]]
    mirror_link.push_agenda([t for t in tasks if t], reminders)


def _reminder_loop():
    while not _reminder_stop.is_set():
        try:
            for item in due_reminders():
                import voice_service
                voice_service.announce("Reminder: " + str(item["title"]))   # spoken and shown on the mirror
                print("[REMINDER DUE]", item["id"], item["title"])
                publish_voice_event({"type":"reminder_due", "id":item["id"],
                                    "title":item["title"], "due_at":item["due_at"]})
        except Exception as exc:
            print("[REMINDER CHECK ERROR]", repr(exc))
        try:
            _push_agenda()
        except Exception as _exc:
            print("[AGENDA PUSH ERROR]", type(_exc).__name__)
        _reminder_stop.wait(5)

@app.on_event("startup")
def start_reminder_loop():
    global _reminder_thread
    _reminder_stop.clear()
    if _reminder_thread is None or not _reminder_thread.is_alive():
        _reminder_thread = threading.Thread(target=_reminder_loop, daemon=True, name="VisionReminders")
        _reminder_thread.start()
    try:
        import google_sync          # tasks and reminders stay the same as the app's Google account
        google_sync.start()
    except Exception as _exc:
        print("[GOOGLE SYNC] not started:", type(_exc).__name__)

@app.on_event("shutdown")
def stop_reminder_loop():
    _reminder_stop.set()
    try:
        import google_sync
        google_sync.stop()
    except Exception:
        pass
