"""Optional pose inference and cautious Home Alone event state machine.

No model is downloaded or camera opened during import. Model presence is explicit;
fall/no-motion alerts are never generated from simple pixel motion alone.
"""

from dataclasses import dataclass
from pathlib import Path

import cv2


@dataclass(frozen=True)
class PoseReading:
    center_x: float
    center_y: float
    horizontal: bool


class PoseEstimator:
    """MediaPipe Tasks Pose Landmarker; requires a local .task model file.

    Pose inference runs only when Home Alone mode is selected. Never substitute
    ordinary motion for a missing pose model.
    """

    def __init__(self, model_path):
        self.error = None
        self.landmarker = None
        path = Path(model_path) if model_path else None
        if not path or not path.is_file():
            self.error = 'Pose model not configured; set POSE_MODEL_PATH to a local .task model'
            return
        try:
            import mediapipe as mp
            options = mp.tasks.vision.PoseLandmarkerOptions(
                base_options=mp.tasks.BaseOptions(model_asset_path=str(path)),
                running_mode=mp.tasks.vision.RunningMode.IMAGE,
                num_poses=1,
                min_pose_detection_confidence=0.65,
                min_pose_presence_confidence=0.65,
            )
            self.landmarker = mp.tasks.vision.PoseLandmarker.create_from_options(options)
            self._mp = mp
        except Exception:
            self.error = 'Pose engine could not initialize; check MediaPipe and model compatibility'

    @property
    def ready(self):
        return self.landmarker is not None

    def read(self, frame):
        if not self.ready:
            return None
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        result = self.landmarker.detect(image)
        if not result.pose_landmarks:
            return None
        points = result.pose_landmarks[0]
        # 11/12 shoulders; 23/24 hips. Require a visible torso.
        indices = (11, 12, 23, 24)
        if any(getattr(points[i], 'visibility', 1.0) < 0.6 for i in indices):
            return None
        shoulders = ((points[11].x + points[12].x) / 2, (points[11].y + points[12].y) / 2)
        hips = ((points[23].x + points[24].x) / 2, (points[23].y + points[24].y) / 2)
        dx, dy = hips[0] - shoulders[0], hips[1] - shoulders[1]
        # A nearly horizontal torso is a *candidate*, never a confirmed fall.
        horizontal = abs(dx) > abs(dy) * 1.25
        return PoseReading((shoulders[0] + hips[0]) / 2,
                           (shoulders[1] + hips[1]) / 2, horizontal)

    def close(self):
        if self.landmarker:
            self.landmarker.close()
            self.landmarker = None


class HomeAloneMonitor:
    """Time-based warnings and advisories based on actual pose observations.

    Does not issue phone calls, play audio or guarantee medical fall detection.
    Confirmed here means only that observation persisted for the requested time.
    """

    def __init__(self, fall_confirmation_seconds=30.0, stillness_seconds=180.0,
                 movement_threshold=0.025, fall_transition_seconds=3.0):
        if fall_confirmation_seconds <= 0 or stillness_seconds <= 0:
            raise ValueError('Home Alone durations must be positive')
        self.fall_confirmation_seconds = fall_confirmation_seconds
        self.stillness_seconds = stillness_seconds
        self.movement_threshold = movement_threshold
        self.fall_transition_seconds = fall_transition_seconds
        self.reset()

    def reset(self):
        self._last_pose = None
        self._last_upright_at = None
        self._last_motion_at = None
        self._pending_kind = None
        self._pending_since = None
        self._alert_sent = False

    @property
    def pending(self):
        return self._pending_kind

    def acknowledge(self):
        # Explicit dismissal resumes observation from a clean baseline.
        self.reset()

    def observe(self, pose, now):
        if pose is None:
            self.reset()  # Missing/obstructed person is not evidence of a fall.
            return None
        previous = self._last_pose
        moving = previous is not None and (
            ((pose.center_x - previous.center_x) ** 2 +
             (pose.center_y - previous.center_y) ** 2) ** 0.5 > self.movement_threshold
        )
        if self._last_motion_at is None or moving:
            self._last_motion_at = now
        if not pose.horizontal:
            self._last_upright_at = now
            if self._pending_kind == 'fall':
                self._pending_kind = None
                self._pending_since = None
                self._alert_sent = False
        if moving and self._pending_kind == 'no_movement':
            self._pending_kind = None
            self._pending_since = None
            self._alert_sent = False
        if (previous is not None and not previous.horizontal and pose.horizontal
                and self._last_upright_at is not None
                and now - self._last_upright_at <= self.fall_transition_seconds):
            self._pending_kind = 'fall'
            self._pending_since = now
            self._alert_sent = False
            self._last_motion_at = now
            self._last_pose = pose
            return 'fall_warning'
        self._last_pose = pose
        if self._pending_kind is None and now - self._last_motion_at >= self.stillness_seconds:
            self._pending_kind = 'no_movement'
            self._pending_since = now
            self._alert_sent = False
            return 'no_movement_warning'
        if self._pending_kind and not self._alert_sent:
            # A fall candidate must remain horizontal and still during confirmation.
            if self._pending_kind == 'fall' and (not pose.horizontal or moving):
                self._pending_kind = None
                self._pending_since = None
                return None
            if now - self._pending_since >= self.fall_confirmation_seconds:
                self._alert_sent = True
                return 'fall_detected' if self._pending_kind == 'fall' else 'no_movement_detected'
        return None
