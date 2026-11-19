"""
google_sync.py - the mirror's tasks and reminders stay the same as the owner's Google account.

    tasks      <->  Google Tasks (default list): add, tick, untick, rename and delete, both ways.
    reminders  <->  Google Calendar (primary). A reminder said to Vision or added in the app becomes
                    an event with a pop-up at that time, so the phone shows it too. Moving or deleting
                    it on either side does the same on the other. Ticking it done in the app removes
                    Vision's own event; an event that came from Google Calendar stays there.
                    Timed events added in Google Calendar for the next 7 days become mirror reminders,
                    so the dashboard shows them and Vision says them on time.

It runs inside the voice assistant's API (api_server.py). Changes made on the mirror reach Google in
about 2 seconds; changes made on Google reach the mirror within 30 seconds, or at once when the phone
app asks (POST /api/google/sync). Nothing happens until Google is connected in the phone app
(google_link.py): the mirror always syncs the account connected there, never a fixed one.
Only counts and errors are logged, never titles.
"""
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta

import google_link
import notes
import reminder_manager

log = logging.getLogger("visionsync.google")

PULL_EVERY = float(os.environ.get("GOOGLE_SYNC_SECONDS", "30"))
CHECK_EVERY = 2.0
WEEK = 7
GRACE = 15           # seconds: something just sent to Google is not taken as deleted there
LOCAL_ONLY = ""      # google_id of items kept on the mirror only (finished before Google was connected)
SKIP_TYPES = ("workingLocation", "outOfOffice", "focusTime")

TASK_COLUMNS = (("google_id", "TEXT"), ("g_title", "TEXT"), ("g_status", "TEXT"), ("g_at", "REAL"))
REMINDER_COLUMNS = (("google_id", "TEXT"), ("g_title", "TEXT"), ("g_due", "TEXT"), ("g_at", "REAL"),
                    ("source", "TEXT"))

# What the mirror still has to send: new items, and items changed since they were last sent.
TASK_WORK = """SELECT id, note, note_date, created_at, status, google_id, g_title, g_status FROM notes
    WHERE google_id IS NULL
       OR (google_id != '' AND (note != IFNULL(g_title, '') OR status != IFNULL(g_status, '')))"""
REMINDER_WORK = """SELECT id, title, due_at, status, google_id, g_title, g_due, source FROM reminders
    WHERE google_id IS NULL
       OR (google_id != '' AND IFNULL(source, 'mirror') = 'mirror' AND (status = 'done'
           OR (status = 'pending' AND (title != IFNULL(g_title, '') OR due_at != IFNULL(g_due, '')))))"""

_state = {"connected": False, "email": "", "last_sync": "", "problem": ""}
_kick = threading.Event()
_stop = threading.Event()
_busy = threading.Lock()
_thread = None
_schema_for = None


@contextmanager
def _db(path):
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _tasks_db():
    return notes.DATABASE


def _reminders_db():
    return reminder_manager.REMINDER_DB


def _update(conn, table, row_id, **values):
    cols = ", ".join("%s = ?" % name for name in values)
    return conn.execute("UPDATE %s SET %s WHERE id = ?" % (table, cols), (*values.values(), row_id)).rowcount


def _forget(conn, table, row_id):
    """Deleted on Google: delete it here too, without sending the delete back."""
    conn.execute("UPDATE %s SET google_id = NULL WHERE id = ?" % table, (row_id,))
    conn.execute("DELETE FROM %s WHERE id = ?" % table, (row_id,))


def _clean(text, empty=""):
    return " ".join(str(text or "").split())[:500] or empty


def _minutes(value):
    """A Google start time as this mirror's local 'YYYY-MM-DDTHH:MM' (like reminders.due_at)."""
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if when.tzinfo is not None:
        when = when.astimezone().replace(tzinfo=None)
    return when.isoformat(timespec="minutes")


def ensure_schema():
    """Adds the Google columns, and triggers that remember deletes for Google. Safe to run again."""
    global _schema_for
    key = (str(_tasks_db()), str(_reminders_db()))
    if _schema_for == key:
        return
    notes.create_database()
    reminder_manager.init_db()
    for path, table, columns, when in ((_tasks_db(), "notes", TASK_COLUMNS, ""),
                                       (_reminders_db(), "reminders", REMINDER_COLUMNS,
                                        " AND OLD.status = 'pending'")):
        with _db(path) as c:
            have = {row[1] for row in c.execute("PRAGMA table_info(%s)" % table)}
            for name, kind in columns:
                if name not in have:
                    c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, kind))
            c.execute("CREATE TABLE IF NOT EXISTS google_trash (google_id TEXT PRIMARY KEY, at REAL)")
            c.execute("CREATE TRIGGER IF NOT EXISTS %s_google_trash AFTER DELETE ON %s "
                      "WHEN IFNULL(OLD.google_id, '') != ''%s BEGIN "
                      "INSERT OR IGNORE INTO google_trash (google_id, at) VALUES (OLD.google_id, strftime('%%s', 'now')); "
                      "END" % (table, table, when))
    _schema_for = key


def _state_file():
    return google_link.TOKEN_FILE.with_name("google_sync.json")


def _drop_synced():
    """Takes the last account's tasks and reminders off the mirror. Nothing is lost: they stay in
    that Google account. Items never sent to Google stay on the mirror."""
    ensure_schema()
    for path, table in ((_tasks_db(), "notes"), (_reminders_db(), "reminders")):
        with _db(path) as c:
            ids = [(row[0],) for row in c.execute(
                "SELECT id FROM %s WHERE IFNULL(google_id, '') != ''" % table)]
            c.executemany("UPDATE %s SET google_id = NULL WHERE id = ?" % table, ids)   # no delete sent to Google
            c.executemany("DELETE FROM %s WHERE id = ?" % table, ids)
            c.execute("DELETE FROM google_trash")


def _same_account():
    """Another Google account was connected in the app: the mirror shows that person's tasks and
    calendar from now on, and the last person's items leave the mirror (they stay in their Google)."""
    who = google_link.account()
    me = who.get("sub") or who.get("email") or ""
    if not me:
        return                                   # not known yet: decide on the next round
    path = _state_file()
    try:
        last = json.loads(path.read_text(encoding="utf-8")).get("account")
    except (OSError, ValueError, AttributeError):
        last = None
    if last == me:
        return
    if last:
        _drop_synced()
        log.warning("[GOOGLE SYNC] another Google account was connected: the mirror now shows its items")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"account": me}), encoding="utf-8")


def link(refresh_token, access=None):
    """POST /api/google/link: the account connected in the phone app becomes the mirror's.
    Waits for a sync round in progress, so two accounts are never mixed in one round."""
    with _busy:
        who = google_link.link(refresh_token, access)
        try:
            ensure_schema()
            _same_account()                     # another person: the last one's items leave now
        except Exception as exc:
            log.warning("[GOOGLE SYNC] account switch: %s", type(exc).__name__)
    kick()
    return who


def unlink():
    """POST /api/google/unlink ("Disconnect" in the app): the mirror stops syncing and takes that
    account's tasks and reminders off (they stay in the person's Google account)."""
    with _busy:
        google_link.forget()
        _drop_synced()
        try:
            _state_file().unlink()
        except FileNotFoundError:
            pass
        _state.update(connected=False, email="", problem="", last_sync="")


# ------------------------------------------------------------------ mirror -> Google
def _push_tasks():
    with _db(_tasks_db()) as c:
        trash = [row[0] for row in c.execute("SELECT google_id FROM google_trash")]
        work = c.execute(TASK_WORK).fetchall()
    for gid in trash:
        google_link.delete_task(gid)
        with _db(_tasks_db()) as c:
            c.execute("DELETE FROM google_trash WHERE google_id = ?", (gid,))
    month_ago = (date.today() - timedelta(days=30)).isoformat()
    for row in work:
        done = row["status"] != "pending"
        if row["google_id"] is None:
            if done or (row["note_date"] or "") < month_ago:      # finished or old: mirror only
                with _db(_tasks_db()) as c:
                    _update(c, "notes", row["id"], google_id=LOCAL_ONLY)
                continue
            day = row["note_date"] or ""
            gid = google_link.add_task(row["note"], due=day if day != (row["created_at"] or "")[:10] else None)
            with _db(_tasks_db()) as c:
                kept = _update(c, "notes", row["id"], google_id=gid, g_title=row["note"],
                               g_status=row["status"], g_at=time.time())
            if not kept:                       # deleted on the mirror meanwhile
                google_link.delete_task(gid)
            continue
        try:
            google_link.update_task(row["google_id"],
                                    title=row["note"] if row["note"] != row["g_title"] else None,
                                    done=done if row["status"] != row["g_status"] else None)
        except google_link.Gone:
            with _db(_tasks_db()) as c:
                _forget(c, "notes", row["id"])
            continue
        with _db(_tasks_db()) as c:
            _update(c, "notes", row["id"], g_title=row["note"], g_status=row["status"], g_at=time.time())


def _push_reminders():
    with _db(_reminders_db()) as c:
        trash = [row[0] for row in c.execute("SELECT google_id FROM google_trash")]
        work = c.execute(REMINDER_WORK).fetchall()
    for gid in trash:
        google_link.delete_event(gid)
        with _db(_reminders_db()) as c:
            c.execute("DELETE FROM google_trash WHERE google_id = ?", (gid,))
    now = datetime.now().isoformat(timespec="minutes")
    for row in work:
        if row["google_id"] is None:
            if row["status"] != "pending" or (row["due_at"] or "") <= now:    # already said: mirror only
                with _db(_reminders_db()) as c:
                    _update(c, "reminders", row["id"], google_id=LOCAL_ONLY)
                continue
            gid = google_link.add_event(row["title"], datetime.fromisoformat(row["due_at"]))
            with _db(_reminders_db()) as c:
                kept = _update(c, "reminders", row["id"], google_id=gid, g_title=row["title"],
                               g_due=row["due_at"], g_at=time.time(), source="mirror")
            if not kept:
                google_link.delete_event(gid)
        elif row["status"] == "done":          # ticked in the app: Vision's own event goes too
            google_link.delete_event(row["google_id"])
            with _db(_reminders_db()) as c:
                _update(c, "reminders", row["id"], google_id=LOCAL_ONLY)
        else:
            try:
                google_link.move_event(row["google_id"], row["title"], datetime.fromisoformat(row["due_at"]))
            except google_link.Gone:
                with _db(_reminders_db()) as c:
                    _forget(c, "reminders", row["id"])
                continue
            with _db(_reminders_db()) as c:
                _update(c, "reminders", row["id"], g_title=row["title"], g_due=row["due_at"], g_at=time.time())


# ------------------------------------------------------------------ Google -> mirror
def _pull_tasks():
    items, complete = google_link.list_tasks()
    google = {it["id"]: it for it in items if it.get("id")}
    now, changed = time.time(), 0
    stamp = datetime.now().isoformat(timespec="seconds")
    with _db(_tasks_db()) as c:
        mine = {row["google_id"]: row for row in c.execute(
            "SELECT id, note, status, google_id, g_title, g_status, g_at FROM notes "
            "WHERE IFNULL(google_id, '') != ''")}
        deleting = {row[0] for row in c.execute("SELECT google_id FROM google_trash")}
        for gid, it in google.items():
            if gid in deleting:                         # deleted on the mirror, not yet on Google
                continue
            title = _clean(it.get("title"))
            status = "completed" if it.get("status") == "completed" else "pending"
            row = mine.get(gid)
            if row is None:
                if title and status == "pending":       # finished ones stay on Google only
                    c.execute("INSERT INTO notes (note, note_date, created_at, status, google_id, g_title, "
                              "g_status, g_at) VALUES (?, ?, ?, 'pending', ?, ?, 'pending', ?)",
                              (title, str(it.get("due") or "")[:10] or date.today().isoformat(),
                               stamp, gid, title, now))
                    changed += 1
                continue
            values = {}
            if title and title != (row["g_title"] or ""):
                values["g_title"] = title
                if row["note"] == (row["g_title"] or ""):     # not renamed on the mirror meanwhile
                    values["note"] = title
            if status != (row["g_status"] or ""):
                values["g_status"] = status
                if row["status"] == (row["g_status"] or ""):
                    values["status"] = status
                    values["completed_at"] = stamp if status == "completed" else None
            if values:
                _update(c, "notes", row["id"], **values)
                changed += ("note" in values or "status" in values)
        if complete:
            for gid, row in mine.items():
                if gid not in google and (row["g_at"] or 0) < now - GRACE:
                    _forget(c, "notes", row["id"])
                    changed += 1
    return changed


def _declined(ev):
    return any(a.get("self") and a.get("responseStatus") == "declined" for a in ev.get("attendees") or [])


def _pull_events():
    now = datetime.now().astimezone().replace(microsecond=0)
    week_end = now + timedelta(days=WEEK)
    made, made_ok = google_link.list_events((now - timedelta(hours=6)).isoformat(), mine=True)
    week, week_ok = google_link.list_events(now.isoformat(), week_end.isoformat())
    google = {}
    for ev in made + week:
        due = _minutes((ev.get("start") or {}).get("dateTime") or "")   # all-day events: no time to say
        if (ev.get("id") and due and ev.get("status") != "cancelled"
                and ev.get("eventType") not in SKIP_TYPES and not _declined(ev)):
            google[ev["id"]] = (ev, due)
    local_now = now.replace(tzinfo=None).isoformat(timespec="minutes")
    oldest = (now - timedelta(hours=5)).replace(tzinfo=None).isoformat(timespec="minutes")
    last = (week_end - timedelta(hours=1)).replace(tzinfo=None).isoformat(timespec="minutes")
    stamp, t, changed = datetime.now().isoformat(timespec="seconds"), time.time(), 0
    with _db(_reminders_db()) as c:
        mine = {row["google_id"]: row for row in c.execute(
            "SELECT id, title, due_at, status, google_id, g_title, g_due, g_at, source FROM reminders "
            "WHERE IFNULL(google_id, '') != ''")}
        deleting = {row[0] for row in c.execute("SELECT google_id FROM google_trash")}
        for gid, (ev, due) in google.items():
            if gid in deleting:
                continue
            title = _clean(ev.get("summary"), "Event")
            row = mine.get(gid)
            if row is None:
                if due > local_now:                     # not started yet: Vision says it on time
                    ours = ((ev.get("extendedProperties") or {}).get("private") or {}).get(google_link.MARK)
                    c.execute("INSERT INTO reminders (title, due_at, status, created_at, google_id, g_title, "
                              "g_due, g_at, source) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
                              (title, due, stamp, gid, title, due, t,
                               "mirror" if ours == "reminder" else "calendar"))
                    changed += 1
                continue
            if row["status"] != "pending":
                continue
            values = {}
            if title != (row["g_title"] or ""):
                values["g_title"] = title
                if row["title"] == (row["g_title"] or ""):
                    values["title"] = title
            if due != (row["g_due"] or ""):
                values["g_due"] = due
                if row["due_at"] == (row["g_due"] or ""):
                    values["due_at"] = due
            if values:
                _update(c, "reminders", row["id"], **values)
                changed += ("title" in values or "due_at" in values)
        for gid, row in mine.items():
            if gid in google or row["status"] != "pending" or (row["g_at"] or 0) > t - GRACE:
                continue
            due = row["due_at"] or ""
            if (row["source"] or "mirror") == "mirror":
                looked = made_ok and due >= oldest
            else:
                looked = week_ok and local_now < due < last
            if looked:                                  # deleted (or declined) on Google
                _forget(c, "reminders", row["id"])
                changed += 1
    return changed


# ------------------------------------------------------------------ running it
def sync_once(pull=True):
    """One round: the mirror's changes go to Google, then (pull=True) Google's come to the mirror.
    Returns how many tasks and reminders Google changed on the mirror."""
    with _busy:
        if not google_link.connected():
            _state.update(connected=False, email="", problem=google_link.account().get("problem", ""))
            return 0
        ensure_schema()
        _same_account()
        steps = [("sending tasks", _push_tasks, False), ("sending reminders", _push_reminders, False)]
        if pull:
            steps += [("Google Tasks", _pull_tasks, True), ("Google Calendar", _pull_events, True)]
        problems, changed = [], 0
        for name, step, counts in steps:
            try:
                n = step()
                changed += (n or 0) if counts else 0
            except google_link.NotConnected:
                raise
            except Exception as exc:
                problems.append("%s: %s" % (name, exc))
        _state.update(connected=True, email=google_link.account().get("email", ""),
                      problem="; ".join(problems)[:400])
        if not problems:
            _state["last_sync"] = datetime.now().isoformat(timespec="seconds")
        return changed


def _has_local_work():
    try:
        with _db(_tasks_db()) as c:
            if (c.execute("SELECT 1 FROM google_trash LIMIT 1").fetchone()
                    or c.execute(TASK_WORK + " LIMIT 1").fetchone()):
                return True
        with _db(_reminders_db()) as c:
            return bool(c.execute("SELECT 1 FROM google_trash LIMIT 1").fetchone()
                        or c.execute(REMINDER_WORK + " LIMIT 1").fetchone())
    except sqlite3.Error:
        return True


def _loop():
    next_pull = retry_at = 0.0
    said = None
    while not _stop.is_set():
        asked = _kick.is_set()
        _kick.clear()
        now = time.time()
        try:
            if google_link.connected():
                pull = asked or now >= next_pull
                if pull or (now >= retry_at and _has_local_work()):
                    sync_once(pull=pull)
                    if pull:
                        next_pull = now + PULL_EVERY
                    retry_at = now + (60 if _state["problem"] else 0)
            else:
                _state.update(connected=False, problem=google_link.account().get("problem", ""))
        except google_link.NotConnected as exc:
            _state.update(connected=False, problem=str(exc))
        except Exception as exc:              # never let the sync stop the voice assistant
            _state["problem"] = "Google sync: %s" % type(exc).__name__
            next_pull = retry_at = now + 60
        if _state["problem"] != said:
            said = _state["problem"]
            if said:
                log.warning("[GOOGLE SYNC] %s", said)
            elif _state["connected"]:
                log.warning("[GOOGLE SYNC] working (%s)", _state["email"] or "connected")
        _kick.wait(CHECK_EVERY)


def status():
    """For the phone app (through the camera server): connected or not, and which pending tasks and
    reminders are on Google ({mirror id: Google id}), so the app shows each one once."""
    who = google_link.account()
    out = {"connected": who["connected"], "email": who.get("email", ""), "last_sync": _state["last_sync"],
           "problem": who.get("problem", "") or (_state["problem"] if who["connected"] else ""),
           "tasks": {}, "reminders": {}}
    try:
        with _db(_tasks_db()) as c:
            out["tasks"] = {str(r[0]): r[1] for r in c.execute(
                "SELECT id, google_id FROM notes WHERE status = 'pending' AND IFNULL(google_id, '') != ''")}
        with _db(_reminders_db()) as c:
            out["reminders"] = {str(r[0]): r[1] for r in c.execute(
                "SELECT id, google_id FROM reminders WHERE status = 'pending' AND IFNULL(google_id, '') != ''")}
    except sqlite3.Error:
        pass
    return out


def kick():
    """Read Google now (the phone app just changed something there)."""
    _kick.set()


def start():
    global _thread
    if os.environ.get("GOOGLE_SYNC", "on").strip().lower() in ("off", "0", "false", "no"):
        return None
    if _thread is None or not _thread.is_alive():
        _stop.clear()
        _thread = threading.Thread(target=_loop, daemon=True, name="VisionGoogleSync")
        _thread.start()
    return _thread


def stop():
    _stop.set()
    _kick.set()
