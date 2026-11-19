import logging
import os
import threading
import time

import cv2

from .detection import MotionDetector

log = logging.getLogger(__name__)


class Camera:
    def __init__(self, config, database):
        self.config = config
        self.database = database
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.enabled = database.setting("camera_enabled", True)
        self.armed = self.enabled and database.setting("security_enabled", False)
        self.jpeg = None
        self.sequence = 0
        self.last_frame = None
        self.motion = False
        self.error = "Camera starting"
        self.thread = None
        self.control_version = 0

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run, name="camera-capture", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        with self.lock:
            self.jpeg, self.last_frame, self.motion = None, None, False
        if self.thread:
            self.thread.join(timeout=4)

    def update(self, camera_enabled=None, security_enabled=None):
        with self.lock:
            enabled = self.enabled if camera_enabled is None else camera_enabled
            armed = self.armed if security_enabled is None else security_enabled
            if not enabled:
                armed = False
            self.database.save_settings({"camera_enabled": enabled, "security_enabled": armed})
            self.enabled, self.armed = enabled, armed
            self.control_version += 1
            if not enabled:
                self.jpeg, self.last_frame, self.motion = None, None, False
        return self.status()

    def status(self):
        with self.lock:
            age = None if self.last_frame is None else max(0, time.monotonic() - self.last_frame)
            online = self.enabled and age is not None and age < 6
            return {
                "camera_id": self.config.camera_id, "camera_name": self.config.camera_name,
                "camera_enabled": self.enabled, "security_enabled": self.armed,
                "camera_online": online, "motion_detected": self.motion if online else False,
                "frame_age_seconds": round(age, 2) if age is not None else None,
                "detector": "pixel_motion", "human_confirmation_available": False,
                "error": None if online else ("Camera disabled" if not self.enabled else self.error or "Frames stopped"),
            }

    def frame(self):
        with self.lock:
            return (self.sequence, self.jpeg) if self.status()["camera_online"] else (self.sequence, None)

    def open_camera(self):
        source = self.config.camera_source
        if source.isdigit():
            methods = {"auto": cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY,
                       "dshow": cv2.CAP_DSHOW, "msmf": cv2.CAP_MSMF}
            if self.config.camera_backend not in methods:
                raise ValueError("CAMERA_BACKEND must be auto, dshow, or msmf")
            cap = cv2.VideoCapture(int(source), methods[self.config.camera_backend])
        else:
            cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG, [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000,
                cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000,
            ])
        if not cap.isOpened():
            cap.release()
            raise RuntimeError("Selected camera could not open")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        return cap

    def run(self):
        cap = None
        detector = MotionDetector(self.config)
        try:
            while not self.stop_event.is_set():
                with self.lock:
                    enabled, armed, control_version = self.enabled, self.armed, self.control_version
                if not enabled:
                    if cap is not None:
                        cap.release()
                        cap = None
                    detector.reset()
                    self.stop_event.wait(0.2)
                    continue
                try:
                    if cap is None:
                        cap = self.open_camera()
                        detector.reset()
                    success, frame = cap.read()
                    if not success or frame is None:
                        raise RuntimeError("Camera stopped sending images")
                    frame = cv2.resize(frame, (640, 480))
                    motion, incident = detector.process(frame, time.monotonic(), armed)
                    encoded, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    if not encoded:
                        raise RuntimeError("Could not encode camera image")
                    jpeg = buffer.tobytes()
                    with self.lock:
                        # A disable/disarm during capture takes effect before publication.
                        if self.stop_event.is_set() or control_version != self.control_version:
                            detector.reset()
                            continue
                        self.jpeg, self.motion = jpeg, motion
                        self.sequence += 1
                        self.last_frame = time.monotonic()
                        self.error = None
                        if incident and self.armed:
                            event_id = self.database.create_incident(self.config, jpeg)
                            log.info("Recorded motion event %s", event_id)
                    self.stop_event.wait(1 / 15)
                except Exception as error:
                    # Never log a source URL: an IP camera URL may contain credentials.
                    log.warning("Camera read failed (%s); retrying selected source", type(error).__name__)
                    with self.lock:
                        self.jpeg, self.last_frame, self.motion = None, None, False
                        self.error = "Camera unavailable; reconnecting"
                    if cap is not None:
                        cap.release()
                        cap = None
                    detector.reset()
                    self.stop_event.wait(2)
        finally:
            if cap is not None:
                cap.release()
            with self.lock:
                self.jpeg, self.last_frame, self.motion = None, None, False
