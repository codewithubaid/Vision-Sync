"""
camera_link.py - Reads the camera / security backend (motion_stream_server.py).

The backend already serves the phone app. The hub reads the same endpoints so
the mirror itself shows Security Mode, Home Alone Mode, the "Are you okay?"
countdown and emergencies (UC-3). The hub also tells the backend which AI mode
is in use, so the app's Status tab shows Cloud / Local (SRS 4.1).
"""

import json
import logging
import time

try:
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError, URLError
except ImportError:  # pragma: no cover
    raise

log = logging.getLogger("hub.camera")

FALL_WINDOW = 30         # seconds, same as security_events.py
MOTIONLESS_WINDOW = 15
RECOVERED_SHOW = 12      # how long the "person recovered" note stays on the mirror

# Order the dashboard uses when more than one alert is open.
SEVERITY = {
    "FALL_DETECTED": 100, "MOTIONLESS_ESCALATION": 95, "FALL_COUNTDOWN": 80,
    "MOTIONLESS_PROMPT": 75, "SECURITY_ALERT": 70, "CAMERA_OFFLINE": 60, "FALL_RECOVERED": 10,
}


def _clean(a, **extra):
    """Keeps the small fields only. Thumbnails stay on the phone side."""
    out = {
        "id": str(a.get("id", "")),
        "type": str(a.get("type", "")),
        "priority": str(a.get("priority", "LOW")),
        "title": str(a.get("title", "")),
        "message": str(a.get("message", "")),
        "timestamp": int(a.get("timestamp", 0) or 0),
    }
    out.update(extra)
    return out


def derive_alerts(active, events, now=None):
    """Active alerts + the timed prompts that only exist in the event history.

    The backend keeps FALL_COUNTDOWN and MOTIONLESS_PROMPT in its history, not in
    the active list, so the countdown is rebuilt here from the newest events.
    """
    now = time.time() if now is None else now
    out = [_clean(a) for a in (active or [])]
    types_open = set(a["type"] for a in out)
    evs = sorted(events or [], key=lambda e: e.get("timestamp", 0))

    def newest(*kinds):
        for e in reversed(evs):
            if e.get("type") in kinds:
                return e
        return None

    # Fall countdown: running unless a recovery or a confirmed fall came after it.
    cd = newest("FALL_COUNTDOWN")
    if cd and "FALL_DETECTED" not in types_open:
        ts = cd.get("timestamp", 0)
        after = newest("FALL_RECOVERED", "FALL_DETECTED")
        closed = after is not None and after.get("timestamp", 0) >= ts
        if not closed and now - ts < FALL_WINDOW + 2:
            out.append(_clean(cd, deadline=ts + int(cd.get("countdown_seconds", FALL_WINDOW)),
                              window=int(cd.get("countdown_seconds", FALL_WINDOW))))

    # Motionless prompt: 15 s to react.
    mp = newest("MOTIONLESS_PROMPT")
    if mp and not (types_open & {"MOTIONLESS_ESCALATION", "FALL_DETECTED"}):
        ts = mp.get("timestamp", 0)
        after = newest("MOTIONLESS_ESCALATION")
        closed = after is not None and after.get("timestamp", 0) >= ts
        if not closed and now - ts < MOTIONLESS_WINDOW + 2:
            out.append(_clean(mp, deadline=ts + MOTIONLESS_WINDOW, window=MOTIONLESS_WINDOW))

    # Recovery note (UC-3 A2), shown for a few seconds.
    rec = newest("FALL_RECOVERED")
    if rec and now - rec.get("timestamp", 0) < RECOVERED_SHOW and "FALL_DETECTED" not in types_open:
        out.append(_clean(rec, until=rec.get("timestamp", 0) + RECOVERED_SHOW))

    out.sort(key=lambda a: (-SEVERITY.get(a["type"], 50), -a["timestamp"]))
    return out


class CameraLink(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self._fail_count = 0

    def _call(self, path, body=None, timeout=3):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = Request(self.cfg.camera_url + path, data=data, headers={
            "X-Mirror-Secret": self.cfg.mirror_secret, "Content-Type": "application/json"})
        resp = urlopen(req, timeout=timeout)
        try:
            raw = resp.read().decode("utf-8")
        finally:
            resp.close()
        return json.loads(raw) if raw.strip() else {}

    def poll(self):
        """Returns (camera_section, alerts_list, voice_muted or None)."""
        if not self.cfg.mirror_secret:
            return ({"reachable": False, "error": "MIRROR_SECRET_KEY is not set"}, None, None)
        try:
            st = self._call("/status")
            active = self._call("/alerts").get("active_alerts", [])
            events = self._call("/events?limit=25").get("events", [])
        except HTTPError as e:
            self._fail_count += 1
            msg = "Wrong MIRROR_SECRET_KEY" if e.code == 401 else "Camera service error %s" % e.code
            return ({"reachable": False, "error": msg}, None, None)
        except (URLError, OSError, ValueError) as e:
            self._fail_count += 1
            # One missed poll is normal while the backend restarts: only report after 3.
            if self._fail_count < 3:
                return (None, None, None)
            return ({"reachable": False, "error": "Camera service is not running"}, None, None)
        self._fail_count = 0
        cam = {
            "reachable": True,
            "error": "",
            "camera_enabled": st.get("camera_enabled") is not False,
            "security_mode": bool(st.get("security_mode")),
            "home_alone_mode": bool(st.get("home_alone_mode")),
            "resolution": st.get("resolution"),
            "backend_ai_mode": st.get("ai_routing_mode"),
            # Sent by the merged camera server; older servers do not have these.
            "camera_online": st.get("camera_online"),
            "fall_detector": st.get("fall_detector"),
        }
        return (cam, derive_alerts(active, events), bool(st.get("voice_muted")))

    def push_ai_mode(self, mode):
        """Keeps the app's Status tab in step with the mirror. Safe to call often."""
        if not self.cfg.mirror_secret:
            return
        try:
            self._call("/settings/ai_mode", {"mode": mode})
        except Exception:
            pass

    def set_resolution(self, preset):
        if not self.cfg.mirror_secret:
            return False
        try:
            self._call("/settings/resolution", {"preset": preset})
            return True
        except Exception as e:
            log.warning("Could not change the stream size: %s", e)
            return False
