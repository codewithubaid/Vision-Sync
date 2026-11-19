"""
demo.py - Scripted tour for demos, the viva and UI work. No hardware, keys or
internet needed:   python3 visionsync_hub.py --demo

A single scene can be shown on request:
    curl -X POST http://127.0.0.1:8765/api/demo/fall
"""

import time
from datetime import datetime, timedelta


def _today_at(hour, minute=0, days=0):
    d = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days)
    return int(time.mktime(d.timetuple()))


def base_weather(stale=False, error=""):
    now = int(time.time())
    return {
        "ok": True, "configured": True, "stale": stale, "error": error, "units": "metric",
        "fetched_at": now - (1500 if stale else 40),
        "city": "Lahore", "temp": 24.3, "feels_like": 23.6, "humidity": 41,
        "temp_min": 17.0, "temp_max": 26.0, "description": "Haze", "condition": "Haze",
        "icon": "50d", "wind": 2.1, "sunrise": _today_at(6, 28), "sunset": _today_at(17, 6),
        "forecast": [
            {"time": now + 3 * 3600, "temp": 22.0, "icon": "02d", "pop": 0.0},
            {"time": now + 6 * 3600, "temp": 19.4, "icon": "01n", "pop": 0.0},
            {"time": now + 9 * 3600, "temp": 17.2, "icon": "01n", "pop": 0.0},
            {"time": now + 12 * 3600, "temp": 16.1, "icon": "03n", "pop": 0.1},
        ],
    }


def base_calendar(stale=False, error=""):
    now = int(time.time())
    soon = now + 50 * 60
    return {
        "ok": True, "configured": True, "stale": stale, "error": error,
        "fetched_at": now - (1500 if stale else 60),
        "events": [
            {"title": "FYP progress meeting with Sir Rauf", "start": soon, "end": soon + 2700,
             "all_day": False, "location": "S-217, FCCU"},
            {"title": "Mirror frame pickup, Hall Road", "start": _today_at(18, 30), "end": _today_at(19, 15),
             "all_day": False, "location": ""},
            {"title": "Compiler Construction quiz", "start": _today_at(9, 0, 1), "end": _today_at(10, 15, 1),
             "all_day": False, "location": "E-025"},
            {"title": "Prototype demo rehearsal", "start": _today_at(14, 0, 2), "end": _today_at(15, 30, 2),
             "all_day": False, "location": ""},
            {"title": "SRS revision due", "start": _today_at(0, 0, 4), "end": _today_at(23, 59, 4),
             "all_day": True, "location": ""},
        ],
    }


def _alert(kind, title, message, priority="HIGH", **extra):
    a = {"id": "demo_%s" % kind.lower(), "type": kind, "priority": priority, "title": title,
         "message": message, "timestamp": int(time.time())}
    a.update(extra)
    return a


def scene_clear(hub):
    s = hub.store
    s.update("weather", base_weather())
    s.update("calendar", base_calendar())
    s.update("system", {"cpu_temp": 52.5, "cpu_percent": 38, "ram_percent": 61, "wifi_dbm": -57,
                        "online": True, "throttled": False})
    s.update("camera", {"reachable": True, "error": "", "camera_enabled": True, "security_mode": False,
                        "home_alone_mode": False, "resolution": "720p"})
    s.replace("alerts", [])
    s.update("voice", {"state": "idle", "heard": "", "reply": "", "message": "", "muted": False,
                       "connected": True, "since": time.time()})
    s.update("ai", {"mode": "cloud", "source": "network"})
    s.replace("setup", {"active": False, "state": "", "ssid": "", "password": "", "url": "", "network": "", "message": ""})
    s.replace("agenda", {"tasks": ["Print the SRS for the viva", "Buy milk"],
                         "reminders": [{"title": "Take medicine", "due_at": __import__("time").strftime("%Y-%m-%dT21:00")}]})


def _setup(hub, state, network="", message=""):
    scene_clear(hub)
    hub.store.update("system", {"online": False, "wifi_dbm": None})
    hub.store.replace("setup", {"active": True, "state": state, "ssid": "VisionSync-Setup",
                                "password": "48207153", "url": "http://10.42.0.1/",
                                "network": network, "message": message})


def scene_wifi_setup(hub):
    _setup(hub, "waiting")


def scene_wifi_wrong_password(hub):
    _setup(hub, "waiting", message="Could not join Ubaid's Wi-Fi. Check the password and try again.")


def scene_wifi_connecting(hub):
    _setup(hub, "connecting", network="Ubaid's Wi-Fi")


def scene_wifi_connected(hub):
    _setup(hub, "connected", network="Ubaid's Wi-Fi")
    hub.store.update("system", {"online": True, "wifi_dbm": -55})


def scene_listening(hub):
    scene_clear(hub)
    hub.apply_voice({"state": "listening"})


def scene_thinking(hub):
    scene_clear(hub)
    hub.apply_voice({"state": "listening"})
    hub.apply_voice({"state": "thinking", "heard": "Do I need a jacket this evening?"})


def scene_speaking(hub):
    scene_thinking(hub)
    hub.apply_voice({"state": "speaking", "mode": "cloud",
                     "reply": "It drops to about 17 degrees after nine, so a light jacket is a good idea. "
                              "No rain is expected tonight."})


def scene_not_heard(hub):
    scene_clear(hub)
    hub.apply_voice({"state": "error", "message": "I did not catch that. Please repeat your question."})


def scene_offline(hub):
    scene_clear(hub)
    s = hub.store
    s.update("system", {"online": False, "wifi_dbm": None})
    s.update("ai", {"mode": "local", "source": "network"})
    s.update("weather", base_weather(stale=True, error="No connection to the weather service"))
    s.update("calendar", base_calendar(stale=True, error="Calendar unavailable, check connection."))


def scene_offline_speaking(hub):
    scene_offline(hub)
    hub.apply_voice({"state": "listening"})
    hub.apply_voice({"state": "speaking", "heard": "What is on my calendar today?",
                     "reply": "You have the FYP progress meeting with Sir Rauf, then the mirror frame pickup at 6:30."})


def scene_security(hub):
    scene_clear(hub)
    hub.store.update("camera", {"security_mode": True})


def scene_intruder(hub):
    scene_security(hub)
    hub.store.replace("alerts", [_alert("SECURITY_ALERT", "Security Alert: Person Detected",
                                        "Human presence confirmed in Security Mode.")])


def scene_home_alone(hub):
    scene_clear(hub)
    hub.store.update("camera", {"home_alone_mode": True})


def scene_fall(hub):
    scene_home_alone(hub)
    now = int(time.time())
    hub.store.replace("alerts", [_alert("FALL_COUNTDOWN", "Fall Detected - Countdown Started",
                                        "Speaker prompt: 'Are you okay?'. 30s countdown active.",
                                        priority="MEDIUM", deadline=now + 30, window=30)])


def scene_fall_recovered(hub):
    scene_home_alone(hub)
    hub.store.replace("alerts", [_alert("FALL_RECOVERED", "Fall Recovery Noted",
                                        "Person resumed upright movement within countdown window.",
                                        priority="LOW", until=int(time.time()) + 12)])


def scene_emergency(hub):
    scene_home_alone(hub)
    hub.store.replace("alerts", [_alert("FALL_DETECTED", "FALL DETECTED",
                                        "No recovery after 30 seconds. Emergency contact sequence initiated.",
                                        priority="CRITICAL")])


def scene_camera_offline(hub):
    scene_security(hub)
    hub.store.update("camera", {"camera_enabled": True})
    hub.store.replace("alerts", [_alert("CAMERA_OFFLINE", "Camera Offline Alert",
                                        "Camera hardware offline or obscured: Camera dropped stream feed",
                                        priority="CRITICAL")])


def scene_hot(hub):
    scene_clear(hub)
    hub.store.update("system", {"cpu_temp": 86.5, "cpu_percent": 93, "throttled": True})
    hub.store.update("camera", {"resolution": "480p"})


def scene_muted(hub):
    scene_clear(hub)
    hub.store.update("voice", {"muted": True})


def scene_setup(hub):
    """What a fresh install looks like before any key is added."""
    scene_clear(hub)
    s = hub.store
    s.replace("weather", {"ok": False, "configured": False, "stale": False, "fetched_at": 0,
                          "error": "Add OPENWEATHER_API_KEY to .env"})
    s.replace("calendar", {"ok": False, "configured": False, "stale": False, "fetched_at": 0, "events": [],
                           "error": "Add GOOGLE_CALENDAR_ICS_URL to .env"})
    s.update("camera", {"reachable": False, "error": "Camera service is not running"})
    s.update("voice", {"connected": False})


SCENES = {
    "clear": scene_clear, "listening": scene_listening, "thinking": scene_thinking,
    "speaking": scene_speaking, "not_heard": scene_not_heard, "offline": scene_offline,
    "offline_speaking": scene_offline_speaking, "security": scene_security, "intruder": scene_intruder,
    "home_alone": scene_home_alone, "fall": scene_fall, "fall_recovered": scene_fall_recovered,
    "emergency": scene_emergency, "camera_offline": scene_camera_offline, "hot": scene_hot,
    "muted": scene_muted, "setup": scene_setup, "wifi_setup": scene_wifi_setup,
    "wifi_wrong_password": scene_wifi_wrong_password, "wifi_connecting": scene_wifi_connecting,
    "wifi_connected": scene_wifi_connected,
}

# (scene, seconds on screen)
TOUR = [
    ("clear", 8), ("listening", 3), ("thinking", 2), ("speaking", 9),
    ("offline", 6), ("offline_speaking", 8), ("clear", 4),
    ("security", 5), ("intruder", 8), ("clear", 3),
    ("home_alone", 4), ("fall", 12), ("fall_recovered", 6),
    ("fall", 10), ("emergency", 8), ("camera_offline", 6), ("hot", 6),
]


class DemoDriver(object):
    def __init__(self, hub):
        self.hub = hub
        self._hold_until = 0.0

    def show(self, name):
        fn = SCENES.get(name)
        if not fn:
            return False
        self._hold_until = time.time() + 120       # pause the tour so the scene stays up
        fn(self.hub)
        return True

    def run(self):
        import os
        scene_clear(self.hub)
        if os.environ.get("HUB_DEMO_TOUR", "1") in ("0", "false", "no"):
            self._hold_until = float("inf")
        stop = self.hub.stop_event
        while not stop.is_set():
            for name, seconds in TOUR:
                while time.time() < self._hold_until and not stop.is_set():
                    stop.wait(0.5)
                if stop.is_set():
                    return
                SCENES[name](self.hub)
                end = time.time() + seconds
                while time.time() < end and time.time() >= self._hold_until and not stop.is_set():
                    stop.wait(0.25)
