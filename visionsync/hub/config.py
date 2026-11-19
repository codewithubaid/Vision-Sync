"""
config.py - VisionSync hub settings.

Everything comes from environment variables or a .env file (NFR-SEC1: no keys in
the source code or in git). Works on Python 3.6+ with the standard library only.
"""

import os


def load_env_file(path):
    """Reads KEY=VALUE lines into os.environ. Real environment variables win."""
    if not path or not os.path.isfile(path):
        return False
    with open(path, "r") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[7:].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            elif " #" in value:
                value = value.split(" #", 1)[0].strip()
            os.environ.setdefault(key, value)
    return True


def _int(name, default):
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name, default):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class Config(object):
    def __init__(self, env_file=None):
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = [env_file, os.environ.get("VS_ENV_FILE"),
                      os.path.join(here, "..", ".env"), os.path.join(here, ".env")]
        self.env_file = None
        for c in candidates:
            if c and load_env_file(c):
                self.env_file = os.path.abspath(c)
                break

        env = os.environ.get

        # --- Weather (OpenWeatherMap free tier) ---
        self.openweather_key = env("OPENWEATHER_API_KEY", "").strip()
        self.city = env("CITY", "Lahore").strip()
        self.country = env("COUNTRY", "PK").strip()
        self.units = env("UNITS", "metric").strip()            # metric = Celsius
        self.weather_interval = max(60, _int("WEATHER_INTERVAL", 600))     # NFR-P2: 10 min

        # --- Calendar (Google Calendar secret iCal address) ---
        self.calendar_url = env("GOOGLE_CALENDAR_ICS_URL", "").strip()
        cal_id = env("GOOGLE_CALENDAR_ID", "").strip()
        if not self.calendar_url and cal_id and "your_" not in cal_id:
            # Only works when the calendar is public. The secret iCal address is better.
            self.calendar_url = ("https://calendar.google.com/calendar/ical/%s/public/basic.ics"
                                 % cal_id.replace("@", "%40"))
        self.calendar_interval = max(30, _int("CALENDAR_INTERVAL", 300))   # NFR-P2: 5 min
        self.calendar_max_events = max(1, _int("CALENDAR_MAX_EVENTS", 5))
        self.calendar_days_ahead = max(1, _int("CALENDAR_DAYS_AHEAD", 30))

        # --- Camera / security backend (motion_stream_server.py) ---
        self.camera_url = env("CAMERA_URL", "http://127.0.0.1:8080").rstrip("/")
        self.mirror_secret = env("MIRROR_SECRET_KEY", "").strip()
        self.camera_poll = max(0.5, _float("CAMERA_POLL_SECONDS", 1.0))

        # --- Hub servers ---
        self.host = env("HUB_HOST", "127.0.0.1")       # local only (NFR-SEC2)
        self.port = _int("HUB_PORT", 8765)
        self.socket_path = env("HUB_SOCKET", "/tmp/visionsync.sock")
        self.cache_dir = os.path.expanduser(env("HUB_CACHE_DIR", "~/.cache/visionsync"))

        # --- Safety (NFR-S1) ---
        self.temp_limit = _float("TEMP_LIMIT_C", 85.0)
        self.temp_resume = _float("TEMP_RESUME_C", 75.0)
        self.thermal_path = env("THERMAL_PATH", "").strip()    # for tests; empty = auto

        # --- Internet check (NFR-P4: notice a drop within 2 s) ---
        self.net_check_seconds = max(0.2, _float("NET_CHECK_SECONDS", 0.5))
        self.net_check_hosts = [h.strip() for h in
                                env("NET_CHECK_HOSTS", "1.1.1.1:53,8.8.8.8:53").split(",") if h.strip()]

        # --- Voice display ---
        self.voice_clear_seconds = max(3, _int("VOICE_CLEAR_SECONDS", 20))   # NFR-SEC3
        self.wake_word = env("WAKE_WORD", "Hey Vision")
        a = env("AMBIENT", "subtle").strip().lower()
        self.ambient = a if a in ("off", "subtle", "vivid") else "subtle"

        self.wifi_control_url = env("WIFI_CONTROL_URL", "http://127.0.0.1:%s" % env("WIFI_CONTROL_PORT", "8770")).rstrip("/")
        self.demo = _bool("HUB_DEMO", False)

    def problems(self):
        """Human-readable list of things that are not configured yet."""
        out = []
        if not self.openweather_key or "your_" in self.openweather_key:
            out.append("OPENWEATHER_API_KEY is not set: the weather widget will say so.")
        if not self.calendar_url:
            out.append("GOOGLE_CALENDAR_ICS_URL is not set: the calendar widget will say so.")
        if not self.mirror_secret:
            out.append("MIRROR_SECRET_KEY is not set: the hub cannot read the camera backend.")
        return out
