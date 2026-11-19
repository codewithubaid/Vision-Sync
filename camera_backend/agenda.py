"""
agenda.py - the mirror's tasks and reminders for the phone app.

The voice assistant keeps them (what was said to Vision, or added from the app) and keeps them the
same as Google Tasks and Google Calendar of the Google account connected in the app (the app hands
that sign-in to the mirror through /agenda/google/link). The camera server passes the app's requests on to it, so the app, the mirror and Google
always show the same list.
Only the camera server talks to the voice assistant (127.0.0.1); the app needs the mirror secret.
"""
import json
import os
import urllib.error
import urllib.request

VOICE_API = os.environ.get("VOICE_API_URL", "http://127.0.0.1:8000").rstrip("/")
OFFLINE = {"error": "The voice assistant is not running on the mirror."}


def call(method, path, body=None, timeout=5):
    """Calls the voice assistant's API. Returns (HTTP status, JSON)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(VOICE_API + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {}
    except Exception:
        return 503, dict(OFFLINE)


def agenda():
    """{"tasks", "reminders", "google"} in a simple shape for the app, or None if unreachable.
    google_id names the same item in Google Tasks / Google Calendar, so the app shows it once."""
    s1, tasks = call("GET", "/api/tasks?view=open")
    if s1 in (400, 422):                            # an older voice assistant
        s1, tasks = call("GET", "/api/tasks?view=pending")
    s2, rems = call("GET", "/api/reminders")
    if s1 != 200 and s2 != 200:
        return None
    s3, google = call("GET", "/api/google", timeout=3)
    google = google if s3 == 200 and isinstance(google, dict) else {}
    task_ids, reminder_ids = google.get("tasks") or {}, google.get("reminders") or {}
    out_tasks = []
    for t in (tasks.get("tasks") or []) if s1 == 200 else []:
        title = str(t.get("note") or t.get("title") or "").strip()
        if title:
            out_tasks.append({"id": t.get("id"), "title": title,
                              "date": str(t.get("note_date") or t.get("date") or ""),
                              "done": str(t.get("status") or "pending").lower() != "pending",
                              "google_id": str(task_ids.get(str(t.get("id"))) or "")})
    out_rems = []
    for r in (rems.get("reminders") or []) if s2 == 200 else []:
        title = str(r.get("title") or "").strip()
        if title and r.get("due_at"):
            out_rems.append({"id": r.get("id"), "title": title, "due_at": str(r.get("due_at")),
                             "status": str(r.get("status") or "pending"),
                             "google_id": str(reminder_ids.get(str(r.get("id"))) or "")})
    return {"tasks": out_tasks, "reminders": out_rems, "google": bool(google.get("connected")),
            "google_email": str(google.get("email") or ""), "google_problem": str(google.get("problem") or "")}


def _id(payload):
    try:
        return int(payload.get("id"))
    except (TypeError, ValueError):
        return -1


def handle_post(path, payload):
    """The app's changes. Returns (HTTP status, JSON), or None if the path is not an agenda one."""
    if path == "/agenda/reminder":
        return call("POST", "/api/reminders", {"title": str(payload.get("title", "")).strip(),
                                                 "due_at": str(payload.get("due_at", ""))})
    if path == "/agenda/reminder/delete":
        return call("DELETE", "/api/reminders/%d" % _id(payload))
    if path == "/agenda/reminder/done":
        return call("POST", "/api/reminders/%d/done" % _id(payload))
    if path == "/agenda/task":
        body = {"note": str(payload.get("title", "")).strip()}
        day = str(payload.get("date") or "").strip()[:10]
        if day:
            body["note_date"] = day
        return call("POST", "/api/tasks", body)
    if path == "/agenda/sync":
        return call("POST", "/api/google/sync")
    if path == "/agenda/google/link":            # the Google account connected in the app
        body = {"refresh_token": str(payload.get("refresh_token") or "")}
        if payload.get("access_token"):
            body["access_token"] = str(payload["access_token"])
        return call("POST", "/api/google/link", body, timeout=25)
    if path == "/agenda/google/unlink":
        return call("POST", "/api/google/unlink", timeout=10)
    if path == "/agenda/google/token":           # a fresh Google token for the app, no Google page
        return call("POST", "/api/google/token", timeout=15)
    if path == "/agenda/task/done":
        return call("POST", "/api/tasks/%d/complete" % _id(payload))
    if path == "/agenda/task/delete":
        return call("DELETE", "/api/tasks/%d" % _id(payload))
    return None
