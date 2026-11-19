#!/usr/bin/env python3
"""
visionsync_hub.py - VisionSync integration hub (runs on the Jetson Nano).

One small always-on service that joins every part of the mirror:

    voice pipeline  --(Unix socket / HTTP)-->  hub  --(Unix socket)-->  MagicMirror2 module
    camera backend  <--(HTTP, secret key)-->   hub  --(SSE)----------->  stand-alone dashboard page
    OpenWeatherMap, Google Calendar  ------->  hub

Run:   python3 visionsync_hub.py            (real mode)
       python3 visionsync_hub.py --demo     (no hardware needed: scripted tour)

Standard library only. Python 3.6+.
"""

import argparse
import json
import logging
import os
import signal
import socket
import sys
import threading
import time

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn
    import queue
except ImportError:  # pragma: no cover
    raise

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config                      # noqa: E402
from state import StateStore                   # noqa: E402
from weather import WeatherService             # noqa: E402
from calendar_ics import CalendarService       # noqa: E402
from camera_link import CameraLink             # noqa: E402
import system as sysinfo                       # noqa: E402
import demo as demo_mod                        # noqa: E402

log = logging.getLogger("hub")

VOICE_STATES = ("idle", "listening", "transcribing", "thinking", "speaking", "error")
AI_MODES = ("cloud", "local", "limited")
ASSET_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".woff2": "font/woff2", ".svg": "image/svg+xml",
    ".png": "image/png", ".json": "application/json",
}


class Hub(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = StateStore()
        self.weather = WeatherService(cfg)
        self.calendar = CalendarService(cfg)
        self.camera = CameraLink(cfg)
        self.thermal = sysinfo.ThermalGuard(cfg.temp_limit, cfg.temp_resume)
        self.cpu = sysinfo.CpuMeter()
        self.stop_event = threading.Event()
        self.threads = []
        self._voice_mode_at = 0.0        # when the voice pipeline last told us its AI mode
        self._voice_mode = None
        self._voice_clients = 0
        self._res_before_throttle = None
        self._lock = threading.Lock()
        self.demo = None
        self.store.update("hub", {"demo": bool(cfg.demo), "wake_word": cfg.wake_word, "ambient": cfg.ambient})
        # Show cached data at once, before the first download finishes.
        self.store.update("weather", self.weather.section())
        self.store.update("calendar", self.calendar.section())

    # ------------------------------------------------------------ voice
    def apply_voice(self, msg):
        """A message from the voice pipeline. Returns (ok, error_text)."""
        if not isinstance(msg, dict):
            return False, "message must be a JSON object"
        state = str(msg.get("state", "")).strip().lower()
        if state == "wake":
            state = "listening"
        if state and state not in VOICE_STATES:
            return False, "state must be one of: " + ", ".join(VOICE_STATES)

        cur = self.store.get("voice")
        new = {}
        if state:
            new["state"] = state
            new["since"] = time.time()
            if state == "listening" and not msg.get("keep"):
                new.update({"heard": "", "reply": "", "message": ""})
            if state == "idle":
                new.update({"heard": "", "reply": "", "message": ""})
        heard = msg.get("heard", msg.get("text"))
        if heard is not None:
            new["heard"] = str(heard)[:400]
        if msg.get("reply") is not None:
            new["reply"] = str(msg["reply"])[:900]
        if msg.get("message") is not None:
            new["message"] = str(msg["message"])[:300]
        if state == "error" and not new.get("message") and not cur.get("message"):
            new["message"] = "I did not catch that. Please repeat your question."
        if new:
            self.store.update("voice", new)

        mode = msg.get("mode")
        if mode is not None:
            mode = str(mode).strip().lower()
            if mode not in AI_MODES:
                return False, "mode must be one of: " + ", ".join(AI_MODES)
            with self._lock:
                self._voice_mode, self._voice_mode_at = mode, time.time()
            self._publish_ai_mode()
        return True, ""

    def _publish_ai_mode(self):
        """Cloud when online, local when offline. The voice pipeline's own report
        wins for two minutes (it knows about API quota), but never over 'offline'."""
        online = self.store.get("system").get("online")
        with self._lock:
            reported = self._voice_mode if time.time() - self._voice_mode_at < 120 else None
        if online is False:
            mode, source = ("limited" if reported == "limited" else "local"), "network"
        elif reported:
            mode, source = reported, "voice"
        else:
            mode, source = "cloud", "network"
        self.store.update("ai", {"mode": mode, "source": source})
        return mode

    def apply_setup(self, msg):
        """Wi-Fi setup screen, sent by wifi/visionsync_wifi.py. Returns (ok, error_text)."""
        if not isinstance(msg, dict):
            return False, "message must be a JSON object"
        state = str(msg.get("state", ""))
        if state not in ("", "waiting", "connecting", "connected", "checking", "error"):
            return False, "unknown setup state"
        clean = {"active": bool(msg.get("active")), "state": state}
        for k, limit in (("ssid", 64), ("password", 64), ("url", 100), ("network", 64), ("message", 200)):
            clean[k] = str(msg.get(k, ""))[:limit]
        self.store.replace("setup", clean)
        return True, ""

    def apply_agenda(self, msg):
        """Pending tasks and upcoming reminders from the voice assistant. Returns (ok, error_text)."""
        if not isinstance(msg, dict):
            return False, "message must be a JSON object"
        tasks, reminders = msg.get("tasks") or [], msg.get("reminders") or []
        if not isinstance(tasks, list) or not isinstance(reminders, list):
            return False, "tasks and reminders must be lists"
        clean = []
        for r in reminders[:6]:
            if isinstance(r, dict) and str(r.get("title", "")).strip():
                clean.append({"title": str(r["title"])[:120], "due_at": str(r.get("due_at", ""))[:25]})
        self.store.replace("agenda", {"tasks": [str(t)[:120] for t in tasks if str(t).strip()][:8],
                                      "reminders": clean})
        return True, ""

    def wifi_command(self, msg):
        """Voice command for Wi-Fi, passed to the Wi-Fi service (it runs as root).
        Returns (http_code, reply). reply["said"] is a sentence the assistant can speak."""
        if not isinstance(msg, dict):
            return 400, {"ok": False, "said": "Bad request."}
        action = str(msg.get("action", ""))
        if action not in ("status", "setup", "connect", "on", "off"):
            return 400, {"ok": False, "said": "Unknown Wi-Fi command."}
        body = {"action": action, "ssid": str(msg.get("ssid", ""))[:64], "password": str(msg.get("password", ""))[:64]}
        try:
            from urllib.request import Request, urlopen
            from urllib.error import HTTPError
            req = Request(self.cfg.wifi_control_url + "/", data=json.dumps(body).encode("utf-8"),
                          headers={"Content-Type": "application/json"})
            try:
                resp = urlopen(req, timeout=5)
                code = resp.getcode()
            except HTTPError as e:
                resp, code = e, e.code
            try:
                return code, json.loads(resp.read().decode("utf-8") or "{}")
            finally:
                resp.close()
        except Exception:
            return 503, {"ok": False, "said": "The Wi-Fi service is not running on this mirror."}

    def voice_client_change(self, delta):
        with self._lock:
            self._voice_clients = max(0, self._voice_clients + delta)
            n = self._voice_clients
        self.store.update("voice", {"connected": n > 0})

    # ------------------------------------------------------------ workers
    def _spawn(self, name, fn):
        def runner():
            while not self.stop_event.is_set():
                try:
                    fn()
                    return
                except Exception:      # a worker must never die for good (NFR-Q5)
                    log.exception("Worker %s crashed. Restarting in 3 s.", name)
                    self.stop_event.wait(3)
        t = threading.Thread(target=runner, name=name)
        t.daemon = True
        t.start()
        self.threads.append(t)

    def _weather_loop(self):
        while not self.stop_event.is_set():
            sec = self.weather.refresh()
            self.store.update("weather", sec)
            # After a failure try again soon, so data comes back quickly once online.
            wait = self.cfg.weather_interval if not sec.get("error") else min(60, self.cfg.weather_interval)
            self._wait_or_online(wait, sec.get("error"))

    def _calendar_loop(self):
        last_fetch = 0.0
        retry = False
        while not self.stop_event.is_set():
            due = self.cfg.calendar_interval if not retry else min(60, self.cfg.calendar_interval)
            if time.time() - last_fetch >= due or self._came_online.is_set():
                sec = self.calendar.refresh()
                last_fetch = time.time()
                retry = bool(sec.get("error"))
            else:
                sec = self.calendar.section()      # drops events that just ended
            self.store.update("calendar", sec)
            self.stop_event.wait(15)

    _came_online = threading.Event()

    def _wait_or_online(self, seconds, had_error):
        """Sleeps, but wakes early when the internet comes back after an error."""
        end = time.time() + seconds
        while not self.stop_event.is_set() and time.time() < end:
            if had_error and self._came_online.is_set():
                return
            self.stop_event.wait(1)

    def _net_loop(self):
        was = None
        while not self.stop_event.is_set():
            up = sysinfo.internet_up(self.cfg.net_check_hosts, timeout=1.0)
            if up != was:
                log.info("Internet is %s", "UP" if up else "DOWN")
                self.store.update("system", {"online": up})
                self._publish_ai_mode()
                if up and was is False:
                    self._came_online.set()
                    threading.Timer(20, self._came_online.clear).start()
                was = up
            self.stop_event.wait(self.cfg.net_check_seconds)

    def _system_loop(self):
        while not self.stop_event.is_set():
            temp = sysinfo.read_cpu_temp(self.cfg.thermal_path)
            change = self.thermal.feed(temp)
            if change == "on":
                self._res_before_throttle = self.store.get("camera").get("resolution")
                self.camera.set_resolution("480p")          # lighter stream while hot
            elif change == "off" and self._res_before_throttle == "720p":
                self.camera.set_resolution("720p")
            self.store.update("system", {
                "cpu_temp": temp,
                "cpu_percent": self.cpu.percent(),
                "ram_percent": sysinfo.read_ram_percent(),
                "wifi_dbm": sysinfo.read_wifi_dbm(),
                "throttled": self.thermal.throttled,
            })
            self.stop_event.wait(5)

    def _camera_loop(self):
        while not self.stop_event.is_set():
            cam, alerts, muted = self.camera.poll()
            if cam is not None:
                self.store.update("camera", cam)
            if alerts is not None:
                self.store.replace("alerts", alerts)
            elif cam is not None and not cam.get("reachable"):
                self.store.replace("alerts", [])
            if muted is not None:
                self.store.update("voice", {"muted": muted})
            if cam and cam.get("reachable"):
                mode = self.store.get("ai").get("mode")
                if cam.get("backend_ai_mode") != mode:
                    self.camera.push_ai_mode(mode)
            self.stop_event.wait(self.cfg.camera_poll)

    def _housekeeping_loop(self):
        """Clears spoken text from the screen (NFR-SEC3) and un-sticks states."""
        while not self.stop_event.is_set():
            v = self.store.get("voice")
            age = time.time() - (v.get("since") or 0)
            st = v.get("state")
            if st in ("speaking", "error") and age > self.cfg.voice_clear_seconds:
                self.store.update("voice", {"state": "idle", "heard": "", "reply": "", "message": ""})
            elif st in ("listening", "transcribing", "thinking") and age > 60:
                self.store.update("voice", {"state": "idle", "heard": "", "reply": "", "message": ""})
            self._publish_ai_mode()
            self.stop_event.wait(1)

    # ------------------------------------------------------------ lifecycle
    def start(self):
        if self.cfg.demo:
            self.demo = demo_mod.DemoDriver(self)
            self._spawn("demo", self.demo.run)
        else:
            self._spawn("weather", self._weather_loop)
            self._spawn("calendar", self._calendar_loop)
            self._spawn("net", self._net_loop)
            self._spawn("system", self._system_loop)
            self._spawn("camera", self._camera_loop)
        self._spawn("housekeeping", self._housekeeping_loop)

    def stop(self):
        self.stop_event.set()


# ================================================================ Unix socket
class SocketServer(object):
    """Newline-delimited JSON over a Unix socket (SRS 4.4 IPC channel)."""

    def __init__(self, hub):
        self.hub = hub
        self.path = hub.cfg.socket_path
        self.sock = None

    def start(self):
        try:
            if os.path.exists(self.path):
                os.unlink(self.path)
        except OSError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        os.chmod(self.path, 0o660)         # this user and group only
        self.sock.listen(16)
        t = threading.Thread(target=self._accept_loop, name="ipc-accept")
        t.daemon = True
        t.start()
        log.info("IPC socket ready at %s", self.path)

    def stop(self):
        try:
            if self.sock:
                self.sock.close()
            if os.path.exists(self.path):
                os.unlink(self.path)
        except OSError:
            pass

    def _accept_loop(self):
        while not self.hub.stop_event.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            t = threading.Thread(target=self._serve, args=(conn,), name="ipc-client")
            t.daemon = True
            t.start()

    def _serve(self, conn):
        hub = self.hub
        send_lock = threading.Lock()
        alive = threading.Event()
        alive.set()
        info = {"role": "", "q": None}

        def send(obj):
            data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
            with send_lock:
                conn.sendall(data)

        def pusher(q):
            last_ping = time.time()
            while alive.is_set() and not hub.stop_event.is_set():
                try:
                    q.get(timeout=1.0)
                    send({"type": "state", "state": hub.store.snapshot()})
                except queue.Empty:
                    if time.time() - last_ping > 10:
                        last_ping = time.time()
                        try:
                            send({"type": "ping"})
                        except OSError:
                            break
                except OSError:
                    break
            alive.clear()
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        buf = b""
        try:
            while alive.is_set():
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 1000000:          # nobody should send this much
                    break
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except ValueError:
                        send({"type": "error", "error": "not valid JSON"})
                        continue
                    kind = msg.get("type")
                    if kind == "hello":
                        role = str(msg.get("role", ""))
                        if role == "voice" and info["role"] != "voice":
                            hub.voice_client_change(+1)
                        info["role"] = role
                        if msg.get("subscribe") and info["q"] is None:
                            info["q"] = hub.store.subscribe()
                            t = threading.Thread(target=pusher, args=(info["q"],), name="ipc-push")
                            t.daemon = True
                            t.start()
                        send({"type": "state", "state": hub.store.snapshot()})
                    elif kind == "voice":
                        ok, err = hub.apply_voice(msg)
                        send({"type": "ok"} if ok else {"type": "error", "error": err})
                    elif kind == "agenda":
                        ok, err = hub.apply_agenda(msg)
                        send({"type": "ok"} if ok else {"type": "error", "error": err})
                    elif kind == "wifi":
                        code, reply = hub.wifi_command(msg)
                        reply["type"] = "wifi"
                        reply["code"] = code
                        send(reply)
                    elif kind == "get":
                        send({"type": "state", "state": hub.store.snapshot()})
                    elif kind == "ping":
                        send({"type": "pong"})
                    else:
                        send({"type": "error", "error": "unknown type"})
        except (OSError, ValueError):
            pass
        finally:
            alive.clear()
            if info["q"] is not None:
                hub.store.unsubscribe(info["q"])
            if info["role"] == "voice":
                hub.voice_client_change(-1)
            try:
                conn.close()
            except OSError:
                pass


# ================================================================ HTTP
class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(hub, web_dir, asset_dir):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "VisionSyncHub/1.0"

        def log_message(self, fmt, *args):       # keep the journal quiet
            pass

        def _json(self, code, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _file(self, base, rel):
            full = os.path.realpath(os.path.join(base, rel))
            if not full.startswith(os.path.realpath(base) + os.sep) or not os.path.isfile(full):
                return self._json(404, {"error": "not found"})
            with open(full, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ASSET_TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                return self._file(web_dir, "index.html")
            if path.startswith("/assets/"):
                return self._file(asset_dir, path[len("/assets/"):])
            if path == "/api/state":
                return self._json(200, hub.store.snapshot())
            if path == "/healthz":
                return self._json(200, {"ok": True, "uptime": int(time.time() - hub.store.get("hub")["started"])})
            if path == "/api/stream":
                return self._stream()
            if path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._json(404, {"error": "not found"})

        def _stream(self):
            """Server-sent events: the full state on every change."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            q = hub.store.subscribe()
            try:
                self.wfile.write(("data: " + json.dumps(hub.store.snapshot()) + "\n\n").encode("utf-8"))
                self.wfile.flush()
                idle = 0
                while not hub.stop_event.is_set():
                    try:
                        q.get(timeout=1.0)
                        self.wfile.write(("data: " + json.dumps(hub.store.snapshot()) + "\n\n").encode("utf-8"))
                        idle = 0
                    except queue.Empty:
                        idle += 1
                        if idle < 15:
                            continue
                        idle = 0
                        self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
            except (OSError, ValueError):
                pass
            finally:
                hub.store.unsubscribe(q)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            try:
                n = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                n = 0
            if n > 100000:
                return self._json(413, {"error": "too large"})
            raw = self.rfile.read(n) if n else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                return self._json(400, {"error": "not valid JSON"})
            if path == "/api/voice":
                ok, err = hub.apply_voice(body)
                return self._json(200 if ok else 400, {"ok": ok, "error": err})
            if path == "/api/agenda":
                ok, err = hub.apply_agenda(body)
                return self._json(200 if ok else 400, {"ok": ok, "error": err})
            if path == "/api/wifi":
                code, reply = hub.wifi_command(body)
                return self._json(code, reply)
            if path == "/api/setup":
                ok, err = hub.apply_setup(body)
                return self._json(200 if ok else 400, {"ok": ok, "error": err})
            if path.startswith("/api/demo/"):
                if not hub.demo:
                    return self._json(403, {"error": "start the hub with --demo to use this"})
                ok = hub.demo.show(path[len("/api/demo/"):])
                return self._json(200 if ok else 404, {"ok": ok, "scenes": sorted(demo_mod.SCENES)})
            return self._json(404, {"error": "not found"})

    return Handler


# ================================================================ main
def main(argv=None):
    ap = argparse.ArgumentParser(description="VisionSync integration hub")
    ap.add_argument("--demo", action="store_true", help="scripted tour, no hardware or keys needed")
    ap.add_argument("--env", help="path to the .env file")
    ap.add_argument("--port", type=int, help="HTTP port (default 8765)")
    ap.add_argument("--socket", help="Unix socket path (default /tmp/visionsync.sock)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    cfg = Config(args.env)
    if args.demo:
        cfg.demo = True
    if args.port:
        cfg.port = args.port
    if args.socket:
        cfg.socket_path = args.socket

    log.info("Settings file: %s", cfg.env_file or "(none found, using environment)")
    if not cfg.demo:
        for p in cfg.problems():
            log.warning(p)

    hub = Hub(cfg)
    here = os.path.dirname(os.path.abspath(__file__))
    web_dir = os.path.join(here, "web")
    asset_dir = os.environ.get("VS_ASSETS") or os.path.join(here, "..", "magicmirror", "modules", "MMM-VisionSync")

    ipc = SocketServer(hub)
    ipc.start()
    httpd = _ThreadingHTTPServer((cfg.host, cfg.port), make_handler(hub, web_dir, asset_dir))
    t = threading.Thread(target=httpd.serve_forever, name="http")
    t.daemon = True
    t.start()
    log.info("Dashboard page and API at http://%s:%d/", cfg.host, cfg.port)

    hub.start()

    # NFR-S3: stop cleanly on Ctrl+C, `systemctl stop` or shutdown.
    def on_signal(signum, _frame):
        log.info("Signal %s: shutting down.", signum)
        hub.stop()
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        while not hub.stop_event.is_set():
            hub.stop_event.wait(0.5)
    finally:
        hub.stop()
        httpd.shutdown()
        httpd.server_close()
        ipc.stop()
        log.info("Hub stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
