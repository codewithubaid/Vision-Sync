"""
state.py - The one shared picture of the mirror ("state") that every screen reads.

Workers (weather, calendar, system, camera, voice) write sections. Listeners
(MagicMirror over the Unix socket, browsers over SSE) get the whole state every
time something changes. Nothing here is written to disk (NFR-SEC3).
"""

import copy
import threading
import time

try:
    import queue
except ImportError:  # pragma: no cover
    import Queue as queue


def initial_state():
    return {
        "weather": {"ok": False, "stale": False, "configured": True, "fetched_at": 0, "error": "Loading weather"},
        "calendar": {"ok": False, "stale": False, "configured": True, "fetched_at": 0, "events": [], "error": "Loading calendar"},
        "system": {"cpu_temp": None, "cpu_percent": None, "ram_percent": None, "wifi_dbm": None,
                   "online": None, "throttled": False},
        "ai": {"mode": "cloud", "source": "network"},
        "voice": {"state": "idle", "heard": "", "reply": "", "message": "", "muted": False,
                  "connected": False, "since": 0},
        "camera": {"reachable": None, "camera_enabled": None, "security_mode": False,
                   "home_alone_mode": False, "resolution": None, "error": ""},
        "alerts": [],
        "setup": {"active": False, "state": "", "ssid": "", "password": "", "url": "", "network": "", "message": ""},
        "agenda": {"tasks": [], "reminders": []},
        "hub": {"started": int(time.time()), "demo": False, "wake_word": "Hey Vision"},
    }


class StateStore(object):
    def __init__(self):
        self._lock = threading.Lock()
        self._state = initial_state()
        self._version = 0
        self._listeners = []

    # ---- writing ----
    def update(self, section, values):
        """Merges `values` into a dict section. Returns True when something changed."""
        with self._lock:
            cur = self._state.get(section)
            if not isinstance(cur, dict):
                changed = cur != values
                self._state[section] = copy.deepcopy(values)
            else:
                changed = False
                for k, v in values.items():
                    if cur.get(k) != v:
                        cur[k] = copy.deepcopy(v)
                        changed = True
            if changed:
                self._version += 1
        if changed:
            self._notify()
        return changed

    def replace(self, section, value):
        with self._lock:
            changed = self._state.get(section) != value
            if changed:
                self._state[section] = copy.deepcopy(value)
                self._version += 1
        if changed:
            self._notify()
        return changed

    # ---- reading ----
    def get(self, section):
        with self._lock:
            return copy.deepcopy(self._state.get(section))

    def snapshot(self):
        with self._lock:
            snap = copy.deepcopy(self._state)
            snap["version"] = self._version
        snap["now"] = int(time.time())
        return snap

    # ---- listeners ----
    def subscribe(self):
        """Returns a queue that receives a token on each change. Size 1: a slow
        listener just skips straight to the newest state."""
        q = queue.Queue(maxsize=1)
        with self._lock:
            self._listeners.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._listeners:
                self._listeners.remove(q)

    def listener_count(self):
        with self._lock:
            return len(self._listeners)

    def _notify(self):
        with self._lock:
            listeners = list(self._listeners)
        for q in listeners:
            try:
                q.put_nowait(1)
            except queue.Full:
                pass
