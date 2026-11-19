"""
motion_stream_server.py - Smart Mirror Camera Streaming & Security Host
Target: Jetson Orin Nano (Linux / UVC USB Webcam)
Compliance: SRS UC-3, F3, M7, M8, NFR-P3, NFR-SEC2

Merged server. ONE program owns the webcam and serves everything on one port:

    Phone app, main tabs      /stream.mjpg /status /alerts /events /mode/* /chat ...
    Phone app, "Direct camera stream" screen      /video_feed   (same picture)
    Mirror dashboard (VisionSync hub)             /status /alerts /events /settings/*
    Push notifications (Firebase, optional)       /devices /notifications/test   (see push.py)

Every address needs the secret key: header "X-Mirror-Secret: <key>" or "?token=<key>".

Run:   MIRROR_SECRET_KEY=your-long-secret python3 motion_stream_server.py
"""

import os
import glob
import hmac
import signal
import time
import json
import logging
import threading
from typing import Optional, Dict, Any
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import cv2
import numpy as np

import assistant
import agenda
from security_events import SecurityEventManager, EventPriority  # noqa: F401
from push import PushSender

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s"
)
logger = logging.getLogger("CameraServer")

# --- Configuration ---
HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
PORT = int(os.environ.get("SERVER_PORT", "8080"))
DEFAULT_SECRET = "glass_connect_secret_key_2026"
# Set your own with:  export MIRROR_SECRET_KEY=something_long   (and put the same in the app)
SECRET_KEY = os.environ.get("MIRROR_SECRET_KEY", "") or DEFAULT_SECRET
# Which camera: "auto" tries /dev/video0..3, a number picks one, or give a
# video file / stream address (useful for testing without a webcam).
CAMERA_SOURCE = os.environ.get("CAMERA_SOURCE", os.environ.get("CAM_INDEX", "auto")).strip()
STREAM_WIDTH = 1280
STREAM_HEIGHT = 720
TARGET_FPS = 30
IDLE_FPS = 5               # nobody watching and no mode on: read slowly to stay cool
JPEG_QUALITY = 75
DETECT_EVERY_N_FRAMES = 3  # run motion/fall detection on every 3rd frame to keep CPU low (NFR-P5)

# Stream size the app can choose (SRS 4.1 "stream resolution toggle").
# Detection always uses the full 720p frame.
STREAM_PRESETS = {"720p": (1280, 720), "480p": (854, 480)}
stream_preset = "720p"


class CameraHardware:
    """Owns the USB webcam. One capture thread; any number of viewers share its frames."""

    def __init__(self, source: str = "auto"):
        self.source = source
        self.cap: Optional[cv2.VideoCapture] = None
        self.is_file = False
        self.running = False
        self.camera_enabled = True
        self.online = False
        self.lock = threading.Lock()
        self.new_frame = threading.Condition(self.lock)
        self.latest_frame: Optional[np.ndarray] = None
        self.sequence = 0
        self._jpeg: Optional[bytes] = None
        self._jpeg_key = None
        self.viewers = 0
        self.thread: Optional[threading.Thread] = None
        self.offline_reported = False

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._capture_loop, name="CameraCaptureWorker", daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        with self.new_frame:
            self.new_frame.notify_all()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        self._release_camera()

    def set_camera_state(self, enabled: bool):
        with self.lock:
            self.camera_enabled = bool(enabled)
            logger.info("Camera enabled status toggled to: %s", self.camera_enabled)
        event_manager.set_detection_paused(not enabled)

    # ---------------------------------------------------------------- device
    def _open(self, src) -> Optional[cv2.VideoCapture]:
        if isinstance(src, int):
            cap = cv2.VideoCapture(src, cv2.CAP_V4L2)
            if not cap.isOpened():
                cap.release()
                cap = cv2.VideoCapture(src)
        else:
            cap = cv2.VideoCapture(src)
        if cap.isOpened():
            return cap
        cap.release()
        return None

    def _init_camera(self) -> bool:
        src = self.source
        if src.lower() == "auto":
            candidates = [0, 1, 2, 3]
        elif src.isdigit():
            candidates = [int(src)]
        else:
            candidates = [src]
        for c in candidates:
            cap = self._open(c)
            if cap is None:
                continue
            self.is_file = not isinstance(c, int)
            if not self.is_file:
                # MJPG first: most UVC webcams only give 720p at 30 fps in MJPG.
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, STREAM_WIDTH)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, STREAM_HEIGHT)
                cap.set(cv2.CAP_PROP_FPS, TARGET_FPS)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)      # always the newest picture
                ok, _ = cap.read()
                if not ok:                               # a /dev/video node that gives no frames
                    cap.release()
                    continue
            self.cap = cap
            logger.info("Camera opened: %s", ("/dev/video%d" % c) if isinstance(c, int) else c)
            return True
        logger.error("No camera could be opened (CAMERA_SOURCE=%s)", src)
        return False

    def _release_camera(self):
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception as e:
                logger.error("Error releasing camera: %s", e)
            self.cap = None

    def _publish(self, frame: np.ndarray, online: bool):
        with self.new_frame:
            self.latest_frame = frame
            self.online = online
            self.sequence += 1
            self.new_frame.notify_all()

    # ---------------------------------------------------------------- loop
    def _capture_loop(self):
        consecutive_failures = 0
        frame_counter = 0
        paused_shown = False

        while self.running:
            if not self.camera_enabled:
                # Camera turned off by the user: let go of the device, so its light goes off too.
                if self.cap is not None:
                    self._release_camera()
                if not paused_shown:
                    blank = np.zeros((STREAM_HEIGHT, STREAM_WIDTH, 3), dtype=np.uint8)
                    cv2.putText(
                        blank, "Camera Paused", (STREAM_WIDTH // 2 - 180, STREAM_HEIGHT // 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, (200, 200, 200), 2, cv2.LINE_AA
                    )
                    self._publish(blank, False)
                    paused_shown = True
                time.sleep(0.2)
                continue
            paused_shown = False

            if self.cap is None or not self.cap.isOpened():
                if not self._init_camera():
                    consecutive_failures += 1
                    self.online = False
                    if consecutive_failures >= 3 and not self.offline_reported:
                        event_manager.report_camera_offline("Camera device disconnected or blocked")
                        self.offline_reported = True
                    time.sleep(2.0)
                    continue

            started = time.time()
            ret, frame = self.cap.read()
            if (not ret or frame is None) and self.is_file:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)          # test video: play again
                ret, frame = self.cap.read()
            if not ret or frame is None:
                consecutive_failures += 1
                logger.warning("Frame read failed (failure count: %d)", consecutive_failures)
                if consecutive_failures >= 5:
                    self._release_camera()
                    self.online = False
                    if not self.offline_reported:
                        event_manager.report_camera_offline("Camera dropped stream feed")
                        self.offline_reported = True
                time.sleep(0.05)
                continue

            consecutive_failures = 0
            if self.offline_reported:
                self.offline_reported = False
                event_manager.report_camera_online()

            # Ensure 1280x720 16:9
            if frame.shape[1] != STREAM_WIDTH or frame.shape[0] != STREAM_HEIGHT:
                frame = cv2.resize(frame, (STREAM_WIDTH, STREAM_HEIGHT))
            self._publish(frame, True)

            # Security / fall detection on every Nth frame, only while a mode is on.
            frame_counter += 1
            busy = event_manager.needs_frames
            if busy and frame_counter % DETECT_EVERY_N_FRAMES == 0:
                try:
                    event_manager.process_frame(frame)
                except Exception:
                    logger.exception("Detection failed on one frame (continuing)")

            # Full speed while someone watches or a mode is on; slow otherwise (less heat).
            fps = TARGET_FPS if (busy or self.viewers > 0) else IDLE_FPS
            time.sleep(max(0.0, 1.0 / fps - (time.time() - started)))

    # ---------------------------------------------------------------- viewers
    def get_jpeg(self) -> Optional[bytes]:
        """Newest frame as JPEG. Encoded only when someone asks, once per frame and size."""
        with self.lock:
            frame, key = self.latest_frame, (self.sequence, stream_preset)
            if frame is None:
                return None
            if self._jpeg_key == key:
                return self._jpeg
        out_w, out_h = STREAM_PRESETS.get(stream_preset, (STREAM_WIDTH, STREAM_HEIGHT))
        out = frame if out_w == frame.shape[1] else cv2.resize(frame, (out_w, out_h))
        ok, enc = cv2.imencode(".jpg", out, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        if not ok:
            return None
        data = enc.tobytes()
        with self.lock:
            self._jpeg, self._jpeg_key = data, key
        return data

    def wait_for_frame(self, last_sequence: int, timeout: float = 1.0) -> int:
        with self.new_frame:
            self.new_frame.wait_for(lambda: self.sequence != last_sequence or not self.running, timeout=timeout)
            return self.sequence


def read_jetson_cpu_temp() -> Optional[float]:
    """Hottest thermal zone in Celsius, or None when the board has no sensor file.
    (No made-up fallback value: the app shows 'unknown' instead.)"""
    best = None
    for path in glob.glob("/sys/devices/virtual/thermal/thermal_zone*/temp") or glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            with open(path, "r") as f:
                v = float(f.read().strip())
        except Exception:
            continue
        if v > 1000:
            v /= 1000.0
        if v <= 0 or v > 150 or abs(v - 100.0) < 0.01:      # some zones report a fixed dummy value
            continue
        best = v if best is None else max(best, v)
    return round(best, 1) if best is not None else None


def get_wifi_signal_dbm() -> Optional[int]:
    """Wi-Fi signal in dBm from /proc/net/wireless, or None (Ethernet / unknown)."""
    try:
        with open("/proc/net/wireless", "r") as f:
            for line in f.readlines()[2:]:
                parts = line.split()
                if len(parts) >= 4:
                    return int(float(parts[3].rstrip(".")))
    except Exception:
        pass
    return None


# Global shared components
camera_hw = CameraHardware(source=CAMERA_SOURCE)
event_manager = SecurityEventManager()
push_sender = PushSender()
event_manager.on_event = push_sender.notify_event


class MirrorRequestHandler(BaseHTTPRequestHandler):
    """HTTP endpoint handling video stream, commands, status, and alerts."""

    def log_message(self, fmt, *args):      # keep the log for events, not every poll
        pass

    def _check_auth(self) -> bool:
        """Enforces secret key authentication per NFR-SEC2."""
        auth_header = self.headers.get("X-Mirror-Secret") or ""
        query = parse_qs(urlparse(self.path).query)
        token_query = query.get("token", [""])[0] or ""
        key = SECRET_KEY.encode("utf-8")
        return (hmac.compare_digest(auth_header.encode("utf-8"), key)
                or hmac.compare_digest(token_query.encode("utf-8"), key))

    def _send_json(self, status_code: int, data: Dict[str, Any]):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _stream(self):
        """MJPEG video (SRS F3, M7, M8, NFR-P3). A frame is sent once, when it is new."""
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        with camera_hw.lock:
            camera_hw.viewers += 1
        sequence = -1
        try:
            while camera_hw.running:
                sequence = camera_hw.wait_for_frame(sequence)
                jpeg = camera_hw.get_jpeg()
                if jpeg is None:
                    continue
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with camera_hw.lock:
                camera_hw.viewers = max(0, camera_hw.viewers - 1)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/healthz":
            # No secret needed, and it gives nothing away: only "the server is up".
            self._send_json(200, {"ok": True})
            return

        if not self._check_auth():
            self._send_json(401, {"error": "Unauthorized: Invalid or missing secret key"})
            return

        if parsed.path in ("/stream.mjpg", "/video_feed"):
            # /video_feed is the address the app's "Direct camera stream" screen uses.
            self._stream()

        elif parsed.path == "/status":
            # Status tab metadata (SRS 4.1)
            status_data = {
                "online": True,
                "camera_enabled": camera_hw.camera_enabled,
                "camera_online": camera_hw.online,
                "security_mode": event_manager.security_mode,
                "home_alone_mode": event_manager.home_alone_mode,
                "cpu_temperature_c": read_jetson_cpu_temp(),
                "ai_routing_mode": event_manager.ai_routing_mode,
                "wifi_signal_dbm": get_wifi_signal_dbm(),
                "active_alerts_count": len(event_manager.get_active_alerts()),
                "resolution": stream_preset,
                "voice_muted": event_manager.voice_muted,
                "fall_detector": event_manager.fall_detector,
                "push_ready": push_sender.ready,
                "push_devices": len(push_sender.devices),
                "push_queued": push_sender.queued,
                "push_error": push_sender.error,
                "viewers": camera_hw.viewers,
                "timestamp": int(time.time()),
            }
            self._send_json(200, status_data)

        elif parsed.path == "/alerts":
            # Returns active and queued security/fall alerts
            active = event_manager.get_active_alerts()
            self._send_json(200, {
                "active_alerts": active,
                "status": "All Clear" if not active else "ALERTS_ACTIVE"
            })

        elif parsed.path == "/events":
            # Returns historical log of security/fall events
            query = parse_qs(parsed.query)
            try:
                limit = max(1, min(200, int(query.get("limit", [50])[0])))
            except ValueError:
                limit = 50
            events = event_manager.get_event_history(limit)
            if query.get("thumbs", ["0"])[0] != "1":
                events = [{k: v for k, v in e.items() if k != "thumbnail"} for e in events]
            self._send_json(200, {"events": events})

        elif parsed.path == "/agenda":
            # The mirror's tasks and reminders (said to Vision, or added in the app)
            data = agenda.agenda()
            if data is None:
                self._send_json(503, dict(agenda.OFFLINE))
            else:
                self._send_json(200, data)

        else:
            self._send_json(404, {"error": "Not Found"})

    def do_POST(self):
        parsed = urlparse(self.path)

        if not self._check_auth():
            self._send_json(401, {"error": "Unauthorized: Invalid or missing secret key"})
            return

        try:
            content_len = max(0, min(1_000_000, int(self.headers.get("Content-Length", 0) or 0)))
        except ValueError:
            content_len = 0
        body = self.rfile.read(content_len).decode("utf-8", "replace") if content_len > 0 else "{}"
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}

        if parsed.path == "/mode/security":
            # Mode switching: Security Mode ON/OFF (SRS UC-3)
            enable = bool(payload.get("enabled", True))
            event_manager.set_security_mode(enable)
            state_str = "ON" if enable else "OFF"
            self._send_json(200, {"message": f"Security Mode: {state_str}", "security_mode": enable})

        elif parsed.path == "/mode/home_alone":
            # Mode switching: Home Alone Mode ON/OFF (SRS UC-3)
            enable = bool(payload.get("enabled", True))
            event_manager.set_home_alone_mode(enable)
            state_str = "ON" if enable else "OFF"
            self._send_json(200, {"message": f"Home Alone Mode: {state_str}", "home_alone_mode": enable})

        elif parsed.path == "/camera/toggle":
            # Real hardware camera toggle command from app
            enabled = bool(payload.get("enabled", True))
            camera_hw.set_camera_state(enabled)
            self._send_json(200, {"message": f"Camera is {'ON' if enabled else 'OFF'}", "camera_enabled": enabled})

        elif parsed.path == "/alerts/dismiss":
            # Dismiss alert acknowledged by user
            alert_id = payload.get("id")
            dismissed = event_manager.dismiss_alert(alert_id)
            self._send_json(200, {"dismissed": dismissed, "status": "All Clear"})

        elif parsed.path == "/chat":
            # Chat screen in the app. Answers questions; never changes settings.
            snapshot = {
                "camera_enabled": camera_hw.camera_enabled,
                "security_mode": event_manager.security_mode,
                "home_alone_mode": event_manager.home_alone_mode,
                "cpu_temperature_c": read_jetson_cpu_temp(),
                "wifi_signal_dbm": get_wifi_signal_dbm(),
            }
            history = payload.get("history", [])
            if not isinstance(history, list):
                history = []
            result = assistant.ask(str(payload.get("message", "")), history, snapshot, event_manager.get_active_alerts())
            event_manager.ai_routing_mode = result["mode"]
            self._send_json(200, result)

        elif parsed.path == "/settings/resolution":
            global stream_preset
            preset = payload.get("preset", "720p")
            if preset not in STREAM_PRESETS:
                self._send_json(400, {"error": "preset must be one of: " + ", ".join(STREAM_PRESETS)})
            else:
                stream_preset = preset
                self._send_json(200, {"resolution": stream_preset})

        elif parsed.path == "/settings/voice_mute":
            # Mutes the voice assistant only. Safety prompts are never muted.
            event_manager.voice_muted = bool(payload.get("muted", True))
            self._send_json(200, {"voice_muted": event_manager.voice_muted})

        elif parsed.path == "/calls/log":
            # The phone reports each emergency call attempt so the Jetson keeps the full timeline
            try:
                attempt = int(payload.get("attempt", 1))
            except (TypeError, ValueError):
                attempt = 1
            event_manager.log_call_attempt(
                target=str(payload.get("target", "")),
                phone=str(payload.get("phone", "")),
                attempt=attempt,
                result=str(payload.get("result", "")),
                incident=str(payload.get("incident", "")),
            )
            self._send_json(200, {"logged": True})

        elif parsed.path == "/devices":
            # The phone app registers its Firebase token here so alerts reach it when closed.
            try:
                count = push_sender.register(str(payload.get("token", "")), bool(payload.get("enabled", True)))
                self._send_json(200, {"registered": bool(payload.get("enabled", True)), "devices": count,
                                      "push_ready": push_sender.ready})
            except ValueError as e:
                self._send_json(400, {"error": str(e)})

        elif parsed.path == "/notifications/test":
            if not push_sender.ready:
                self._send_json(503, {"error": push_sender.error or "Push notifications are not set up"})
            elif not push_sender.devices:
                self._send_json(409, {"error": "No phone registered yet. Open the app once while connected to the mirror."})
            else:
                push_sender.enqueue("VisionSync test", "Push notifications from your mirror are working.",
                                    {"type": "TEST"})
                self._send_json(200, {"queued": True, "devices": len(push_sender.devices)})

        elif parsed.path == "/settings/ai_mode":
            # AI routing mode (local / cloud / limited). The mirror hub keeps this up to date.
            mode = str(payload.get("mode", "local"))
            if mode not in ("local", "cloud", "limited"):
                self._send_json(400, {"error": "mode must be local, cloud or limited"})
            else:
                event_manager.ai_routing_mode = mode
                self._send_json(200, {"ai_routing_mode": mode})

        elif parsed.path.startswith("/agenda/"):
            # The app adds, completes or deletes the mirror's tasks and reminders
            result = agenda.handle_post(parsed.path, payload)
            if result is None:
                self._send_json(404, {"error": "Endpoint not found"})
            else:
                self._send_json(*result)

        else:
            self._send_json(404, {"error": "Endpoint not found"})


def run_server():
    if SECRET_KEY == DEFAULT_SECRET:
        logger.warning("Using the DEFAULT secret key. Set MIRROR_SECRET_KEY before real use (NFR-SEC1).")
    camera_hw.start()
    event_manager.start()
    push_sender.start()
    server = ThreadingHTTPServer((HOST, PORT), MirrorRequestHandler)
    logger.info("Mirror Camera & Security Server listening on http://%s:%d", HOST, PORT)

    # `systemctl stop` sends SIGTERM: shut down as cleanly as Ctrl+C does.
    def _on_term(signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, _on_term)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Server shutting down...")
        camera_hw.stop()
        event_manager.stop()
        push_sender.stop()
        server.server_close()


if __name__ == "__main__":
    run_server()
