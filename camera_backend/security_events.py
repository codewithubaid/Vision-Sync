"""
security_events.py - Event Processing, Pose Verification & Alert Engine
Target: Jetson Orin Nano (Python 3.8+)
Compliance: SRS UC-3, F3, M7, M8, NFR-P3, NFR-SEC2

Merged version. The alert types, texts and timings are the ones the phone app
and the mirror dashboard already use. What changed:

  * Falls are detected with the MediaPipe pose model (pose_fall.py) when it is
    available, as UC-3 requires. Without MediaPipe or a model file the older
    shape-based check is used, and /status says so ("fall_detector").
  * With the pose model, "Are you okay?" for a motionless person is only asked
    while a person is actually in view. Before, an empty room triggered it.
  * A camera-offline alert clears by itself when the camera comes back.
"""

import os
import time
import json
import subprocess
import base64
import logging
import threading
from enum import Enum
from typing import Dict, Any, List, Optional
import cv2
import numpy as np

from pose_fall import PoseEstimator, PoseReading

logger = logging.getLogger("SecurityEvents")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class EventPriority(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class EventType(str, Enum):
    SECURITY_ALERT = "SECURITY_ALERT"
    FALL_COUNTDOWN = "FALL_COUNTDOWN"
    FALL_DETECTED = "FALL_DETECTED"
    FALL_RECOVERED = "FALL_RECOVERED"
    MOTIONLESS_PROMPT = "MOTIONLESS_PROMPT"
    MOTIONLESS_ESCALATION = "MOTIONLESS_ESCALATION"
    CAMERA_OFFLINE = "CAMERA_OFFLINE"
    CAMERA_ONLINE = "CAMERA_ONLINE"
    PET_SHADOW_FILTERED = "PET_SHADOW_FILTERED"
    ALL_CLEAR = "ALL_CLEAR"
    CALL_ATTEMPT = "CALL_ATTEMPT"


class SecurityEventManager:
    """Manages Security Mode, Home Alone Mode, Fall Countdowns, and Event Logging."""

    def __init__(self, log_path: str = "security_events.log", use_pose: Optional[bool] = None,
                 pose_estimator=None):
        """use_pose: None = decide from FALL_DETECTOR (auto | pose | shape), True/False = force.
        pose_estimator: inject a ready estimator (used by the tests)."""
        self.log_path = log_path
        self.security_mode: bool = False
        self.home_alone_mode: bool = False
        self.ai_routing_mode: str = "local"  # "local" or "cloud"

        # Timings (seconds). The defaults are the SRS / app values.
        self.fall_countdown_seconds = _env_float("FALL_COUNTDOWN_SECONDS", 30.0)
        self.motionless_seconds = _env_float("MOTIONLESS_SECONDS", 60.0)
        self.motionless_grace_seconds = _env_float("MOTIONLESS_GRACE_SECONDS", 15.0)
        self.fall_transition_seconds = _env_float("FALL_TRANSITION_SECONDS", 3.0)
        self.person_gone_seconds = _env_float("PERSON_GONE_SECONDS", 20.0)
        self.pose_interval = 1.0 / max(0.5, _env_float("POSE_FPS", 4.0))

        # Background subtractor for initial motion detection
        self.bg_subtractor = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=25, detectShadows=True)

        # Pose model (optional)
        if pose_estimator is not None:
            self.pose = pose_estimator
        else:
            choice = os.environ.get("FALL_DETECTOR", "auto").strip().lower()
            want = use_pose if use_pose is not None else choice != "shape"
            self.pose = PoseEstimator() if want else None
        self._last_pose_time: float = 0.0
        self._last_pose: Optional[PoseReading] = None
        self._last_upright_time: float = 0.0
        self._last_person_time: float = 0.0
        self._flat_streak: int = 0
        self._upright_streak: int = 0
        self._unconfirmed_motion_since: float = 0.0
        self._warmup_frames: int = 0     # frames seen since detection (re)started

        # Fall & Motionless state machines
        self.fall_state: str = "IDLE"  # IDLE, COUNTDOWN_30S, CONFIRMED
        self.fall_start_time: float = 0.0
        self.last_motion_time: float = time.time()
        self.motionless_prompt_given: bool = False
        self.motionless_prompt_time: float = 0.0

        # Alert collections
        self.active_alerts: Dict[str, Dict[str, Any]] = {}
        self.event_history: List[Dict[str, Any]] = []

        self.lock = threading.Lock()
        self._log_lock = threading.Lock()
        self.running = False

        # Pause flag, voice mute flag, and limits so a 24 h run stays healthy
        self.detection_paused: bool = False
        self.voice_muted: bool = False
        self._last_quiet_log: float = 0.0
        self._last_sec_alert_time: float = 0.0
        self.max_history: int = 500
        self.max_log_bytes: int = 5_000_000
        self.worker_thread: Optional[threading.Thread] = None
        self.last_all_clear_broadcast: float = time.time()
        # Called with every logged (non-silent) event. The server points this at the
        # push-notification sender. It must return quickly.
        self.on_event = None

    # ------------------------------------------------------------------ info
    @property
    def pose_ready(self) -> bool:
        return self.pose is not None and self.pose.ready

    @property
    def fall_detector(self) -> str:
        """'pose' = MediaPipe pose model (SRS UC-3), 'shape' = older outline check."""
        return "pose" if self.pose_ready else "shape"

    @property
    def needs_frames(self) -> bool:
        """False when nothing would be done with a frame (lets the camera idle)."""
        return (self.security_mode or self.home_alone_mode) and not self.detection_paused

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        self.running = True
        self.worker_thread = threading.Thread(target=self._monitor_loop, name="EventEngineWorker", daemon=True)
        self.worker_thread.start()
        logger.info("SecurityEventManager started. Fall detector: %s%s", self.fall_detector,
                    " (%s)" % self.pose.model_name if self.pose_ready else "")

    def stop(self):
        self.running = False
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=2.0)
        if self.pose is not None and hasattr(self.pose, "close"):
            self.pose.close()

    def _reset_tracking(self):
        """Call with the lock held: forget everything about the person in view."""
        self.fall_state = "IDLE"
        self.motionless_prompt_given = False
        self.last_motion_time = time.time()
        self._last_pose = None
        self._last_upright_time = 0.0
        self._last_person_time = 0.0
        self._flat_streak = 0
        self._upright_streak = 0
        self._unconfirmed_motion_since = 0.0
        self._warmup_frames = 0

    def set_security_mode(self, enabled: bool):
        with self.lock:
            if enabled and not self.security_mode and not self.home_alone_mode:
                self._warmup_frames = 0          # detection was idle: let the background settle
            self.security_mode = bool(enabled)
            self._unconfirmed_motion_since = 0.0
            logger.info("Security Mode set to %s", enabled)

    def set_home_alone_mode(self, enabled: bool):
        with self.lock:
            self.home_alone_mode = bool(enabled)
            # Start from a clean slate. Without this, an old "last motion" time
            # makes the mirror ask "Are you okay?" right after the mode is switched on.
            self._reset_tracking()
            logger.info("Home Alone Mode set to %s", enabled)

    def set_detection_paused(self, paused: bool):
        """Called when the user turns the camera off/on. While paused, nothing is
        'motionless' and nothing can trigger an alert."""
        with self.lock:
            self.detection_paused = bool(paused)
            self._reset_tracking()
            logger.info("Detection paused: %s", paused)

    # ------------------------------------------------------------------ frames
    def _frame_to_thumbnail_b64(self, frame: np.ndarray) -> str:
        """Generates compact base64 thumbnail for fast low-latency mobile alert."""
        thumb = cv2.resize(frame, (320, 180))
        _, enc = cv2.imencode(".jpg", thumb, [int(cv2.IMWRITE_JPEG_QUALITY), 65])
        return base64.b64encode(enc.tobytes()).decode("utf-8")

    def _estimate_pose_and_verify_human(self, frame: np.ndarray, bbox) -> Dict[str, Any]:
        """Shape-based fallback: outline size and aspect ratio of the moving region."""
        x, y, w, h = bbox
        aspect_ratio = float(w) / max(1, h)

        # Standing human: aspect ratio < 0.8
        # Fallen human: bounding box lies flat (aspect ratio >= 1.25)
        is_human = (h > 100) or (w > 120 and h > 50)
        is_horizontal = aspect_ratio >= 1.25

        return {
            "is_human": is_human,
            "is_fallen": is_horizontal,
            "confidence": 0.88 if is_human else 0.35,
            "bbox": [x, y, w, h]
        }

    def process_frame(self, frame: np.ndarray):
        """Called for captured camera frames (every Nth frame, full 720p)."""
        now = time.time()
        if self.detection_paused:
            return
        fg_mask = self.bg_subtractor.apply(frame)
        # The first frames after detection starts look like "everything moved" to the
        # background model. Let it settle for about a second before acting on it.
        self._warmup_frames += 1
        warming_up = self._warmup_frames <= 12

        # Remove shadows (MOG2 shadow pixels are gray = 127)
        _, thresh = cv2.threshold(fg_mask, 200, 255, cv2.THRESH_BINARY)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)

        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        significant_motion = False
        largest_contour = None
        max_area = 0

        for c in ([] if warming_up else contours):
            area = cv2.contourArea(c)
            if area > 1800:
                significant_motion = True
                if area > max_area:
                    max_area = area
                    largest_contour = c

        # With the pose model in Home Alone mode, only the person's own movement counts
        # (a TV or a fan must not hide a motionless person). Otherwise any movement does.
        if significant_motion and not (self.pose_ready and self.home_alone_mode):
            with self.lock:
                self.last_motion_time = now

        if self.pose_ready:
            self._process_with_pose(frame, now, significant_motion, max_area)
            return

        # ---------------- shape-based fallback (no pose model) ----------------
        if not significant_motion:
            return

        x, y, w, h = cv2.boundingRect(largest_contour)
        pose_res = self._estimate_pose_and_verify_human(frame, (x, y, w, h))

        # Quiet cases: small animals, pet movement, shadows
        if not pose_res["is_human"] or max_area < 3500:
            self._log_quiet_event(EventType.PET_SHADOW_FILTERED, "Low contour motion filtered (pet/shadow)")
            return

        with self.lock:
            # A person moved: cancel any pending "are you okay?" escalation (SRS A3)
            self.motionless_prompt_given = False

        if self.security_mode:
            self._trigger_security_alert(self._frame_to_thumbnail_b64(frame))
        if self.home_alone_mode:
            self._evaluate_home_alone_rules(pose_res, self._frame_to_thumbnail_b64(frame))

    def _process_with_pose(self, frame: np.ndarray, now: float, motion: bool, motion_area: float):
        """Pose-model path. Runs the model a few times per second, and only when needed:
        always in Home Alone mode (a still person must be seen too), and in
        Security mode only while something is moving."""
        need = self.home_alone_mode or (self.security_mode and motion)
        if not need or now - self._last_pose_time < self.pose_interval:
            return
        self._last_pose_time = now
        reading = self.pose.read(frame)

        if self.security_mode and motion:
            if reading is not None:
                self._unconfirmed_motion_since = 0.0
                self._trigger_security_alert(self._frame_to_thumbnail_b64(frame))
            elif motion_area >= 3500:
                # Large movement but no body recognised (dark room, person partly hidden).
                # Do not stay silent for ever: after 5 s of it, alert anyway and say so.
                if not self._unconfirmed_motion_since:
                    self._unconfirmed_motion_since = now
                elif now - self._unconfirmed_motion_since >= 5.0:
                    self._unconfirmed_motion_since = 0.0
                    self._trigger_security_alert(
                        self._frame_to_thumbnail_b64(frame),
                        title="Security Alert: Movement Detected",
                        message="Sustained movement in Security Mode. A person could not be confirmed.")
            else:
                self._log_quiet_event(EventType.PET_SHADOW_FILTERED, "Low contour motion filtered (pet/shadow)")
        elif not motion:
            self._unconfirmed_motion_since = 0.0

        if self.home_alone_mode:
            self.observe_pose(reading, now, frame)

    def observe_pose(self, reading: Optional[PoseReading], now: float, frame: Optional[np.ndarray] = None):
        """Home Alone rules from one pose reading. Public so the tests can drive it."""
        start_countdown = False
        recovered = False
        with self.lock:
            previous = self._last_pose
            self._last_pose = reading
            if reading is None:
                # Nobody recognised. This is not evidence of a fall or of recovery.
                self._flat_streak = 0
                self._upright_streak = 0
                return
            self._last_person_time = now

            # Did the body move since the last reading? (keeps "motionless" honest)
            if previous is not None:
                shift = ((reading.center_x - previous.center_x) ** 2 +
                         (reading.center_y - previous.center_y) ** 2) ** 0.5
                if shift > 0.025:
                    self.last_motion_time = now
                    if self.fall_state != "COUNTDOWN_30S":
                        self.motionless_prompt_given = False

            if reading.horizontal:
                self._upright_streak = 0
                self._flat_streak += 1
                # A fall = upright a moment ago, lying now, seen twice in a row.
                # Someone who lay down long ago (sofa, bed) does not start a countdown.
                if (self.fall_state == "IDLE" and self._flat_streak >= 2 and self._last_upright_time
                        and now - self._last_upright_time <= self.fall_transition_seconds + self.pose_interval * 2):
                    start_countdown = True
            else:
                self._flat_streak = 0
                self._upright_streak += 1
                self._last_upright_time = now
                if self.fall_state == "COUNTDOWN_30S" and self._upright_streak >= 2:
                    recovered = True

        if start_countdown:
            thumb = self._frame_to_thumbnail_b64(frame) if frame is not None else ""
            self._evaluate_home_alone_rules({"is_fallen": True}, thumb)
        elif recovered:
            self._evaluate_home_alone_rules({"is_fallen": False}, "")

    # ------------------------------------------------------------------ alerts
    def _trigger_security_alert(self, thumbnail_b64: str, title: str = "Security Alert: Person Detected",
                                message: str = "Human presence confirmed in Security Mode."):
        with self.lock:
            # One alert per 30 s, so a person walking around does not create a flood
            if time.time() - self._last_sec_alert_time < 30.0:
                return
            self._last_sec_alert_time = time.time()

            alert_id = f"sec_{int(time.time() * 1000)}"
            alert = {
                "id": alert_id,
                "type": EventType.SECURITY_ALERT,
                "priority": EventPriority.HIGH,
                "title": title,
                "message": message,
                "thumbnail": thumbnail_b64,
                "timestamp": int(time.time()),
                "status": "ACTIVE"
            }
            self.active_alerts[alert_id] = alert
            self._append_event_log(alert)
            logger.warning("SECURITY ALERT TRIGGERED: %s", alert_id)

    def _evaluate_home_alone_rules(self, pose_res: Dict[str, Any], thumbnail_b64: str):
        now = time.time()
        with self.lock:
            if pose_res["is_fallen"]:
                if self.fall_state == "IDLE":
                    self.fall_state = "COUNTDOWN_30S"
                    self.fall_start_time = now
                    countdown_event = {
                        "id": f"fall_cd_{int(now * 1000)}",
                        "type": EventType.FALL_COUNTDOWN,
                        "priority": EventPriority.MEDIUM,
                        "title": "Fall Detected - Countdown Started",
                        "message": "Speaker prompt: 'Are you okay?'. %ds countdown active." % int(self.fall_countdown_seconds),
                        "thumbnail": thumbnail_b64,
                        "timestamp": int(now),
                        "countdown_seconds": int(self.fall_countdown_seconds)
                    }
                    self._append_event_log(countdown_event)
                    self._play_speaker_prompt("Are you okay?")
            else:
                if self.fall_state == "COUNTDOWN_30S":
                    self.fall_state = "IDLE"
                    self.last_motion_time = now
                    recovered_note = {
                        "id": f"rec_{int(now * 1000)}",
                        "type": EventType.FALL_RECOVERED,
                        "priority": EventPriority.LOW,
                        "title": "Fall Recovery Noted",
                        "message": "Person resumed upright movement within countdown window.",
                        "timestamp": int(now)
                    }
                    self._append_event_log(recovered_note)
                    logger.info("Fall recovered in time. Logged as low-priority note.")

    def _play_speaker_prompt(self, message: str):
        """Speaks through the mirror speaker (needs: sudo apt install espeak).
        Safety prompts are never muted. Voice mute only affects the voice assistant.
        If espeak is missing it just logs."""
        logger.info("[MIRROR SPEAKER AUDIO]: %s", message)

        def _speak():
            try:
                subprocess.run(["espeak", message], timeout=20, check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

        threading.Thread(target=_speak, name="SpeakerWorker", daemon=True).start()

    def _monitor_loop(self):
        while self.running:
            now = time.time()
            with self.lock:
                if self.home_alone_mode and self.fall_state == "COUNTDOWN_30S":
                    elapsed = now - self.fall_start_time
                    if elapsed >= self.fall_countdown_seconds:
                        self.fall_state = "CONFIRMED"
                        fall_id = f"fall_{int(now * 1000)}"
                        fall_alert = {
                            "id": fall_id,
                            "type": EventType.FALL_DETECTED,
                            "priority": EventPriority.CRITICAL,
                            "title": "FALL DETECTED",
                            "message": "No recovery after %d seconds. Emergency contact sequence initiated." % int(self.fall_countdown_seconds),
                            "timestamp": int(now),
                            "status": "ACTIVE"
                        }
                        self.active_alerts[fall_id] = fall_alert
                        self._append_event_log(fall_alert)
                        self._play_speaker_prompt("Emergency. A fall was detected. Help is being called.")
                        logger.error("CRITICAL FALL DETECTED: %s", fall_id)

                if self.detection_paused:
                    self.last_motion_time = now
                    self.motionless_prompt_given = False

                # With the pose model, "motionless" needs a person in view. When nobody
                # has been seen for a while the room is empty: nothing to ask.
                if (self.pose_ready and self.home_alone_mode and not self.motionless_prompt_given
                        and now - self._last_person_time > self.person_gone_seconds):
                    self.last_motion_time = now

                has_open_emergency = any(
                    a.get("type") in (EventType.MOTIONLESS_ESCALATION, EventType.FALL_DETECTED)
                    for a in self.active_alerts.values()
                )

                if (self.home_alone_mode and not self.detection_paused
                        and self.fall_state not in ("CONFIRMED", "COUNTDOWN_30S")):
                    inactive_seconds = now - self.last_motion_time
                    if (inactive_seconds >= self.motionless_seconds and not self.motionless_prompt_given
                            and not has_open_emergency):
                        self.motionless_prompt_given = True
                        self.motionless_prompt_time = now
                        self._play_speaker_prompt("Are you okay?")
                        prompt_event = {
                            "id": f"prompt_{int(now * 1000)}",
                            "type": EventType.MOTIONLESS_PROMPT,
                            "priority": EventPriority.MEDIUM,
                            "title": "Motionless Prompt",
                            "message": "Mirror speaker prompted: 'Are you okay?'. %d-second grace period." % int(self.motionless_grace_seconds),
                            "timestamp": int(now)
                        }
                        self._append_event_log(prompt_event)

                    elif self.motionless_prompt_given and (now - self.motionless_prompt_time >= self.motionless_grace_seconds):
                        esc_id = f"motionless_{int(now * 1000)}"
                        esc_alert = {
                            "id": esc_id,
                            "type": EventType.MOTIONLESS_ESCALATION,
                            "priority": EventPriority.HIGH,
                            "title": "Unresponsive Person Alert",
                            "message": "No movement %d seconds after speaker prompt. Contacting primary contact." % int(self.motionless_grace_seconds),
                            "timestamp": int(now),
                            "status": "ACTIVE"
                        }
                        self.active_alerts[esc_id] = esc_alert
                        self._append_event_log(esc_alert)
                        self._play_speaker_prompt("No response. Contacting your emergency contact.")
                        self.motionless_prompt_given = False

                if not self.active_alerts and (now - self.last_all_clear_broadcast >= 60.0):
                    self.last_all_clear_broadcast = now
                    all_clear_event = {
                        "id": f"allclear_{int(now * 1000)}",
                        "type": EventType.ALL_CLEAR,
                        "priority": EventPriority.LOW,
                        "title": "All Clear",
                        "message": "System nominal. No active alerts or safety incidents.",
                        "timestamp": int(now)
                    }
                    self._append_event_log(all_clear_event, silent=True)

            time.sleep(0.5)

    def report_camera_offline(self, reason: str):
        with self.lock:
            if any(a.get("type") == EventType.CAMERA_OFFLINE for a in self.active_alerts.values()):
                return                      # already reported, do not stack duplicates
            alert_id = f"offline_{int(time.time() * 1000)}"
            alert = {
                "id": alert_id,
                "type": EventType.CAMERA_OFFLINE,
                "priority": EventPriority.CRITICAL,
                "title": "Camera Offline Alert",
                "message": f"Camera hardware offline or obscured: {reason}",
                "timestamp": int(time.time()),
                "status": "ACTIVE"
            }
            self.active_alerts[alert_id] = alert
            self._append_event_log(alert)
            logger.critical("CAMERA OFFLINE ALERT: %s", reason)

    def report_camera_online(self):
        """The camera works again: close the offline alert and note it in the history."""
        with self.lock:
            stale = [k for k, a in self.active_alerts.items() if a.get("type") == EventType.CAMERA_OFFLINE]
            if not stale:
                return
            for k in stale:
                del self.active_alerts[k]
            self._reset_tracking()
            self._append_event_log({
                "id": f"online_{int(time.time() * 1000)}",
                "type": EventType.CAMERA_ONLINE,
                "priority": EventPriority.LOW,
                "title": "Camera Back Online",
                "message": "Camera feed restored. Monitoring resumed.",
                "timestamp": int(time.time()),
            })
            logger.info("Camera back online. Offline alert cleared.")

    def dismiss_alert(self, alert_id: Optional[str]) -> bool:
        with self.lock:
            self.last_motion_time = time.time()
            self.motionless_prompt_given = False
            if not alert_id:
                self.active_alerts.clear()
                self.fall_state = "IDLE"
                return True
            if alert_id in self.active_alerts:
                del self.active_alerts[alert_id]
                if not self.active_alerts:
                    self.fall_state = "IDLE"
                return True
            return False

    def log_call_attempt(self, target: str, phone: str, attempt: int, result: str, incident: str = ""):
        """The phone app makes the emergency calls. It reports each attempt here so
        the full timeline lives in the Jetson log (SRS UC-3 step 7). Phone is masked."""
        masked = ("***" + phone[-3:]) if phone else ""
        now = time.time()
        event = {
            "id": f"call_{int(now * 1000)}",
            "type": EventType.CALL_ATTEMPT,
            "priority": EventPriority.LOW,
            "title": f"Call attempt: {target}",
            "message": f"{target} {masked}, attempt {attempt}: {result}. {incident}".strip(),
            "timestamp": int(now),
        }
        with self.lock:
            self._append_event_log(event)

    def get_active_alerts(self) -> List[Dict[str, Any]]:
        with self.lock:
            return list(self.active_alerts.values())

    def get_event_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.lock:
            return self.event_history[-limit:]

    def _log_quiet_event(self, event_type: EventType, message: str):
        now = time.time()
        if now - self._last_quiet_log < 30.0:  # at most one filtered-event line per 30 s
            return
        self._last_quiet_log = now
        event = {
            "id": f"quiet_{int(time.time() * 1000)}",
            "type": event_type,
            "priority": EventPriority.LOW,
            "title": "Filtered Event",
            "message": message,
            "timestamp": int(time.time())
        }
        self._append_event_log(event, silent=True)

    def _append_event_log(self, event: Dict[str, Any], silent: bool = False):
        # Silent events (All Clear, filtered pets/shadows) go to the file only,
        # so they never push real events out of the app's history list.
        if not silent:
            self.event_history.append(event)
            if len(self.event_history) > self.max_history:
                del self.event_history[: len(self.event_history) - self.max_history]
            if self.on_event is not None:
                try:
                    self.on_event(event)
                except Exception as e:
                    logger.error("Event listener failed: %s", e)
        try:
            with self._log_lock:
                if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > self.max_log_bytes:
                    os.replace(self.log_path, self.log_path + ".1")
                with open(self.log_path, "a") as f:
                    # The picture stays in memory for the app; the log file keeps text only.
                    f.write(json.dumps({k: v for k, v in event.items() if k != "thumbnail"}) + "\n")
        except Exception as e:
            logger.error("Failed to write event log to disk: %s", e)
