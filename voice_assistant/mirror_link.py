"""
mirror_link.py - joins the voice assistant to the rest of VisionSync.

* Loads the shared settings file visionsync/.env (one place for every key, NFR-SEC1).
* Tells the router at once whether the internet is up (UC-4, NFR-P4: the hub
  checks every 0.5 s; without the hub a 1 s probe is used).
* Gives live mirror data (time, weather, calendar) and the camera controls.

Nothing here raises: without the hub or the camera the assistant keeps working.
"""
import importlib.util
import json
import os
import socket
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
HUB_DIR = os.path.join(HERE, "..", "visionsync", "hub")


def load_env(path):
    """KEY=VALUE lines into os.environ. Real environment variables win."""
    try:
        with open(path) as f:
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
                os.environ.setdefault(key, value)
        return True
    except OSError:
        return False


ENV_FILE = None
for _path in (os.environ.get("VS_ENV_FILE"), os.path.join(HERE, "..", "visionsync", ".env"),
              os.path.join(HERE, ".env")):
    if _path and os.path.isfile(_path) and load_env(_path):
        ENV_FILE = os.path.abspath(_path)
        break


# Some networks have broken IPv6, and calls to the cloud then hang. NET_FORCE_IPV4=1 in
# visionsync/.env makes this process look up IPv4 addresses only (doctor.py says when it helps).
_FORCE_V4 = [False]
_orig_getaddrinfo = socket.getaddrinfo


def _getaddrinfo(host, port, family=0, *args, **kwargs):
    if _FORCE_V4[0] and family in (0, socket.AF_UNSPEC):
        family = socket.AF_INET
    return _orig_getaddrinfo(host, port, family, *args, **kwargs)


def force_ipv4(on=True):
    _FORCE_V4[0] = bool(on)


socket.getaddrinfo = _getaddrinfo
force_ipv4(os.environ.get("NET_FORCE_IPV4", "").strip() == "1")

_SESSION = [None]


def http():
    """One shared HTTPS connection pool: cloud calls reuse a connection instead of opening a new one."""
    if _SESSION[0] is None:
        import requests
        _SESSION[0] = requests.Session()
    return _SESSION[0]


def warm_openai():
    """Opens the connection to OpenAI in the background at the wake word, while the user is speaking."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return

    def _go():
        try:
            http().get("https://api.openai.com/v1/models", headers={"Authorization": "Bearer " + key},
                       timeout=(3, 5))
        except Exception:
            pass

    threading.Thread(target=_go, daemon=True).start()


def _load_client_class():
    """The hub's own visionsync_client.py when the folders sit side by side, else the copy here."""
    for path in (os.path.join(HUB_DIR, "visionsync_client.py"), os.path.join(HERE, "visionsync_client.py")):
        if os.path.isfile(path):
            try:
                spec = importlib.util.spec_from_file_location("visionsync_client", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module.VisionSync
            except Exception:
                continue
    return None


VisionSync = _load_client_class()

_lock = threading.Lock()
_client = None
_net = {"at": 0.0, "up": None}


def client():
    """Shared hub connection for answers, reminders and the phone chat."""
    global _client
    with _lock:
        if _client is None and VisionSync is not None:
            _client = VisionSync(role="assistant", timeout=0.5)
        return _client


def _probe():
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            socket.create_connection((host, 53), timeout=1.0).close()
            return True
        except OSError:
            continue
    return False


def online():
    """True / False. The hub's answer when it runs, else a quick probe (kept 2 s)."""
    c = client()
    if c is not None:
        up = c.online()
        if up is not None:
            return bool(up)
    now = time.time()
    if now - _net["at"] > 2.0:
        _net["up"], _net["at"] = _probe(), now
    return bool(_net["up"])


def state():
    """The dashboard's full state, or None when the hub is not running."""
    c = client()
    return c.state(max_age=1.0) if c is not None else None


def context():
    """Live facts for the language model: time, weather, next events."""
    c = client()
    text = c.context() if c is not None else ""
    return text or time.strftime("Now: %A %d %B %Y, %I:%M %p")


def report_mode(mode):
    """cloud / local / limited, shown on the mirror and in the phone app."""
    c = client()
    if c is not None and mode in ("cloud", "local", "limited"):
        c.set_mode(mode)


def push_agenda(tasks, reminders):
    c = client()
    if c is not None and hasattr(c, "agenda"):
        c.agenda(tasks, reminders)


def show_reply(text):
    c = client()
    if c is not None:
        c.speaking(text)


def camera(path, body=None):
    """Calls the camera server with the shared secret. Returns its JSON, or None."""
    url = os.environ.get("CAMERA_URL", "http://127.0.0.1:8080").rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "X-Mirror-Secret": os.environ.get("MIRROR_SECRET_KEY", ""), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return None
