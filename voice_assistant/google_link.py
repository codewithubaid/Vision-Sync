"""
google_link.py - the mirror's connection to the Google account connected in the phone app.

No account is set on the mirror. Whoever taps "Connect Google" in the app (Supabase Google sign-in,
offline access) is the account the mirror uses: the app hands over Google's long-lived sign-in
(refresh token) once, through the camera server, protected by the mirror secret. Connecting another
account in the app switches the mirror to that account; "Disconnect" in the app unlinks it.

The mirror only needs VisionSync's own Google OAuth client secret - the same Web client that
Supabase uses for "Connect Google" - as GOOGLE_CLIENT_SECRET in visionsync/.env (install.sh asks
for it). That identifies the VisionSync app to Google, not any person. The client ID is read from
the app's token, so it never has to be typed.

google_sync.py uses this module to read and change Google Tasks (default list) and Google Calendar
(primary calendar). The sign-in is kept in ~/.config/visionsync/google_token.json (only this user
can read it).    Check:  python3 google_link.py status
"""
import json
import os
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

TOKEN_FILE = Path(os.environ.get("GOOGLE_TOKEN_FILE")
                  or Path.home() / ".config" / "visionsync" / "google_token.json")
SCOPES = ("https://www.googleapis.com/auth/tasks", "https://www.googleapis.com/auth/calendar.events")
TOKEN_URL = "https://oauth2.googleapis.com/token"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
TASKS_URL = "https://tasks.googleapis.com/tasks/v1/lists/@default/tasks"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
TIMEOUT = float(os.environ.get("GOOGLE_TIMEOUT", "10"))
MARK = "visionsync"     # private property on the calendar events the mirror makes

NOT_LINKED = "not synced with Google yet: connect Google in the phone app"
NO_SECRET = ("the mirror needs VisionSync's Google client secret (GOOGLE_CLIENT_SECRET): "
             "run install.sh again and paste it")
BAD_SECRET = ("the Google client secret on the mirror is not the one of the app's Google client: "
              "run install.sh again and paste the right one")
EXPIRED = "Google ended the mirror's sign-in: tap Sync mirror in the app"
NO_SCOPES = "Google did not allow Tasks and Calendar: tap Sync mirror in the app and tick every box"


class GoogleError(Exception):
    """Google said no, or could not be reached."""

    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


class NotConnected(GoogleError):
    """No usable Google sign-in on the mirror."""


class Gone(GoogleError):
    """The task or event is not on Google any more."""


_lock = threading.RLock()
_cache = {"token": None, "mtime": None, "path": None}
_session = None


def _http():
    global _session
    if _session is None:
        import requests
        _session = requests.Session()
    return _session


def _request(method, url, **kw):
    """One HTTPS call to Google (the tests replace this)."""
    kw.setdefault("timeout", TIMEOUT)
    return _http().request(method, url, **kw)


def _json(reply):
    try:
        data = reply.json() if reply.content else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _q(ident):
    return urllib.parse.quote(str(ident), safe="")


# ------------------------------------------------------------------ the saved sign-in
def _load():
    """The saved sign-in (read again whenever the file changes)."""
    try:
        mtime = TOKEN_FILE.stat().st_mtime_ns
    except OSError:
        _cache.update(token={}, mtime=None, path=str(TOKEN_FILE))
        return {}
    if _cache["token"] is None or _cache["mtime"] != mtime or _cache["path"] != str(TOKEN_FILE):
        try:
            tok = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            tok = {}
        _cache.update(token=tok if isinstance(tok, dict) else {}, mtime=mtime, path=str(TOKEN_FILE))
    return dict(_cache["token"])


def _save(tok):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_FILE.with_name(TOKEN_FILE.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(tok, f, indent=1)
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(TOKEN_FILE))
    _cache.update(token=dict(tok), mtime=TOKEN_FILE.stat().st_mtime_ns, path=str(TOKEN_FILE))


def _set_problem(tok, problem):
    if tok.get("problem", "") != problem:
        if problem:
            tok["problem"] = problem
        else:
            tok.pop("problem", None)
        _save(tok)


def connected():
    """True while the mirror holds a Google sign-in from the app."""
    with _lock:
        return bool(_load().get("refresh_token"))


def account():
    """{"connected", "email", "sub", "problem", "linked_at"}: the account the mirror syncs."""
    with _lock:
        tok = _load()
    return {"connected": bool(tok.get("refresh_token")), "email": tok.get("email", ""),
            "sub": tok.get("sub", ""), "problem": tok.get("problem", ""), "linked_at": tok.get("linked_at", "")}


def _client(tok):
    """VisionSync's Google OAuth client: its ID comes from the app's token, the secret from .env."""
    return (tok.get("client_id") or os.environ.get("GOOGLE_CLIENT_ID", "").strip(),
            os.environ.get("GOOGLE_CLIENT_SECRET", "").strip())


def _refresh(tok):
    """A new access token from the saved sign-in (needs the client secret). Call with _lock held."""
    client_id, secret = _client(tok)
    if not (client_id and secret):
        _set_problem(tok, NO_SECRET)
        raise NotConnected(NO_SECRET)
    try:
        reply = _request("POST", TOKEN_URL, data={"client_id": client_id, "client_secret": secret,
                                                  "refresh_token": tok["refresh_token"],
                                                  "grant_type": "refresh_token"})
    except Exception as exc:
        raise GoogleError("Google could not be reached (%s)" % type(exc).__name__)
    body = _json(reply)
    if reply.status_code == 200 and body.get("access_token"):
        tok.update(access_token=body["access_token"], expiry=time.time() + int(body.get("expires_in") or 3600))
        tok.pop("problem", None)
        _save(tok)
        return tok["access_token"]
    error = str(body.get("error") or "")
    if error == "invalid_grant":                 # signed out, password changed, or 7 days in testing
        for key in ("refresh_token", "access_token", "expiry"):
            tok.pop(key, None)
        tok["problem"] = EXPIRED
        _save(tok)
        raise NotConnected(EXPIRED, reply.status_code)
    if error in ("invalid_client", "unauthorized_client", "deleted_client"):
        _set_problem(tok, BAD_SECRET)
        raise NotConnected(BAD_SECRET, reply.status_code)
    raise GoogleError("Google sign-in refresh failed: HTTP %s %s" % (reply.status_code, error), reply.status_code)


def access_token(min_left=60):
    """A valid Google access token for the linked account."""
    with _lock:
        tok = _load()
        if not tok.get("refresh_token"):
            raise NotConnected(tok.get("problem") or NOT_LINKED)
        if tok.get("access_token") and float(tok.get("expiry") or 0) > time.time() + min_left:
            return tok["access_token"]
        return _refresh(tok)


def app_token():
    """For the phone app: an access token with almost its whole hour left, so the app does not
    have to open the Google page every hour. Only ever for the linked account."""
    token = access_token(min_left=3300)
    with _lock:
        tok = _load()
    return {"access_token": token, "expires_in": max(0, int(float(tok.get("expiry") or 0) - time.time())),
            "email": tok.get("email", "")}


def _tokeninfo(token):
    try:
        reply = _request("GET", TOKENINFO_URL, params={"access_token": token})
    except Exception:
        return {}
    return _json(reply) if reply.status_code == 200 else {}


def link(refresh_token, access=None):
    """The Google account connected in the phone app becomes the mirror's. Returns account()."""
    refresh_token = str(refresh_token or "").strip()
    access = str(access or "").strip()
    if len(refresh_token) < 10:
        raise GoogleError("no Google sign-in was given", 400)
    info = _tokeninfo(access) if access else {}
    if info and not set(SCOPES) <= set(str(info.get("scope") or "").split()):
        raise GoogleError(NO_SCOPES, 403)
    with _lock:
        tok = {"client_id": str(info.get("azp") or info.get("aud") or os.environ.get("GOOGLE_CLIENT_ID", "")).strip(),
               "refresh_token": refresh_token, "email": str(info.get("email") or ""),
               "sub": str(info.get("sub") or ""), "linked_at": datetime.now().isoformat(timespec="minutes")}
        left = int(info.get("expires_in") or 0)
        if access and left > 120:              # the app's own token works for the rest of its hour
            tok.update(access_token=access, expiry=time.time() + left)
        _save(tok)
        try:
            fresh = _refresh(tok)               # proves the mirror can keep the sign-in by itself
        except NotConnected as exc:
            if str(exc) == EXPIRED:
                raise GoogleError("Google did not accept this sign-in: connect Google again in the app", 400)
            fresh = ""                          # no or wrong secret: kept, and shown as the problem
    if fresh and not (tok.get("email") and tok.get("sub")):
        more = _tokeninfo(fresh)
        if more:
            with _lock:
                tok = _load()
                tok.update(email=tok.get("email") or str(more.get("email") or ""),
                           sub=tok.get("sub") or str(more.get("sub") or ""))
                _save(tok)
    return account()


def forget():
    """Unlinked in the app: the mirror forgets the sign-in (the app's own stays as it is)."""
    with _lock:
        try:
            TOKEN_FILE.unlink()
        except FileNotFoundError:
            pass
        _cache.update(token=None, mtime=None, path=None)


def api(method, url, params=None, body=None):
    """One Google API call with the mirror's sign-in. Returns the reply as a dict ({} if empty)."""
    for attempt in range(2):
        tok = access_token()
        try:
            reply = _request(method, url, params=params, json=body,
                             headers={"Authorization": "Bearer " + tok})
        except Exception as exc:
            raise GoogleError("Google could not be reached (%s)" % type(exc).__name__)
        if reply.status_code == 401 and attempt == 0:
            with _lock:                         # the access token ended early: get a new one, once
                saved = _load()
                if saved.get("access_token") == tok:
                    saved["expiry"] = 0
                    _save(saved)
            continue
        if reply.status_code in (404, 410):
            raise Gone("not on Google any more", reply.status_code)
        if reply.status_code >= 400:
            err = _json(reply).get("error")
            msg = err.get("message", "") if isinstance(err, dict) else str(err or "")
            if reply.status_code == 403 and "insufficient" in msg.lower():
                raise GoogleError(NO_SCOPES, 403)
            raise GoogleError("HTTP %d %s" % (reply.status_code, msg[:200]), reply.status_code)
        return _json(reply)
    raise GoogleError("Google refused the mirror's sign-in", 401)


# ------------------------------------------------------------------ Google Tasks
def list_tasks():
    """Every task in the default list. Returns (tasks, complete)."""
    items, page = [], None
    for _ in range(10):
        params = {"showCompleted": "true", "showHidden": "true", "maxResults": "100",
                  "fields": "items(id,title,status,due,updated),nextPageToken"}
        if page:
            params["pageToken"] = page
        reply = api("GET", TASKS_URL, params=params)
        items += reply.get("items") or []
        page = reply.get("nextPageToken")
        if not page:
            return items, True
    return items, False


def add_task(title, due=None, done=False):
    body = {"title": title, "status": "completed" if done else "needsAction"}
    if due:
        body["due"] = str(due)[:10] + "T00:00:00.000Z"      # Google keeps the day only
    return api("POST", TASKS_URL, body=body).get("id", "")


def update_task(task_id, title=None, done=None):
    body = {}
    if title is not None:
        body["title"] = title
    if done is not None:
        body["status"] = "completed" if done else "needsAction"
        if not done:
            body["completed"] = None
    if body:
        api("PATCH", TASKS_URL + "/" + _q(task_id), body=body)


def delete_task(task_id):
    try:
        api("DELETE", TASKS_URL + "/" + _q(task_id))
    except Gone:
        pass


# ------------------------------------------------------------------ Google Calendar
def list_events(time_min, time_max=None, mine=False):
    """Events of the primary calendar that end after time_min (and start before time_max).
    mine=True: only the events the mirror made. Returns (events, complete)."""
    items, page = [], None
    for _ in range(4):
        params = {"singleEvents": "true", "maxResults": "250", "timeMin": time_min,
                  "fields": "items(id,status,summary,start,end,eventType,extendedProperties,"
                            "attendees(self,responseStatus)),nextPageToken"}
        if time_max:
            params["timeMax"] = time_max
        if mine:
            params["privateExtendedProperty"] = MARK + "=reminder"
        if page:
            params["pageToken"] = page
        reply = api("GET", EVENTS_URL, params=params)
        items += reply.get("items") or []
        page = reply.get("nextPageToken")
        if not page:
            return items, True
    return items, False


def _when(start):
    """A datetime with this computer's time zone (reminders keep local times)."""
    start = start if isinstance(start, datetime) else datetime.fromisoformat(str(start))
    return start.astimezone() if start.tzinfo is None else start


def _times(start, minutes):
    start = _when(start)
    return ({"dateTime": start.isoformat(timespec="seconds")},
            {"dateTime": (start + timedelta(minutes=minutes)).isoformat(timespec="seconds")})


def add_event(title, start, minutes=15):
    """A calendar event for a mirror reminder, with a pop-up on the phone at that time."""
    begin, end = _times(start, minutes)
    body = {"summary": title, "start": begin, "end": end,
            "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 0}]},
            "extendedProperties": {"private": {MARK: "reminder"}}}
    return api("POST", EVENTS_URL, body=body).get("id", "")


def move_event(event_id, title, start, minutes=15):
    begin, end = _times(start, minutes)
    api("PATCH", EVENTS_URL + "/" + _q(event_id), body={"summary": title, "start": begin, "end": end})


def delete_event(event_id):
    try:
        api("DELETE", EVENTS_URL + "/" + _q(event_id))
    except Gone:
        pass


def _main(argv):
    info = account()
    if "--quiet" not in argv:
        if info["connected"]:
            print("Google: synced with %s%s" % (info["email"] or "the account connected in the app",
                                                ("  (problem: %s)" % info["problem"]) if info["problem"] else ""))
        else:
            print("Google: %s" % (info["problem"] or NOT_LINKED))
    return 0 if info["connected"] else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
