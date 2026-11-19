"""
pose_fall.py - Body-pose reading for fall detection (SRS UC-3: "pose estimation model").

Based on the partner backend's safety.py (MediaPipe Pose Landmarker), with two changes:
  * the torso angle is measured in pixels, so a wide 16:9 frame does not distort it;
  * a second cue (the body's outline is wider than it is tall) catches falls where
    the torso points towards or away from the camera.

If MediaPipe or the model file is missing, `ready` is False and the server
falls back to the older shape-based check. It never crashes because of this.
"""

import logging
import os
from typing import Optional

import cv2

logger = logging.getLogger("PoseFall")

_HERE = os.path.dirname(os.path.abspath(__file__))
# Looked up in this order. Put pose_landmarker_full.task in models/ for best accuracy.
MODEL_CANDIDATES = ("pose_landmarker_full.task", "pose_landmarker_heavy.task", "pose_landmarker_lite.task")


def find_model(explicit: str = "") -> str:
    """Returns the model path to use, or '' when none is present."""
    if explicit:
        return explicit if os.path.isfile(explicit) else ""
    for folder in (os.path.join(_HERE, "models"), _HERE):
        for name in MODEL_CANDIDATES:
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                return path
    return ""


class PoseReading(object):
    """What the pose model saw in one frame."""
    __slots__ = ("center_x", "center_y", "horizontal", "torso_visible")

    def __init__(self, center_x: float, center_y: float, horizontal: bool, torso_visible: bool = True):
        self.center_x = center_x          # 0..1 across the frame
        self.center_y = center_y          # 0..1 down the frame
        self.horizontal = horizontal      # body is lying rather than upright
        self.torso_visible = torso_visible

    def __repr__(self):
        return "PoseReading(x=%.2f, y=%.2f, horizontal=%s)" % (self.center_x, self.center_y, self.horizontal)


def classify(points, width: int, height: int, min_visibility: float = 0.5) -> Optional[PoseReading]:
    """Turns 33 landmarks into a PoseReading. Kept separate so it can be tested
    without a camera. Returns None when no usable body is in view."""
    def vis(i):
        return getattr(points[i], "visibility", 1.0) or 0.0

    seen = [p for i, p in enumerate(points) if vis(i) >= min_visibility]
    if len(seen) < 6:
        return None
    xs = [p.x * width for p in seen]
    ys = [p.y * height for p in seen]
    box_w, box_h = max(xs) - min(xs), max(ys) - min(ys)
    cx = (sum(p.x for p in seen) / len(seen))
    cy = (sum(p.y for p in seen) / len(seen))

    # 11/12 = shoulders, 23/24 = hips
    torso_ok = all(vis(i) >= min_visibility for i in (11, 12, 23, 24))
    if torso_ok:
        sx = (points[11].x + points[12].x) / 2 * width
        sy = (points[11].y + points[12].y) / 2 * height
        hx = (points[23].x + points[24].x) / 2 * width
        hy = (points[23].y + points[24].y) / 2 * height
        dx, dy = abs(hx - sx), abs(hy - sy)
        torso_flat = dx > dy * 1.25                       # more than ~51 degrees from vertical
        # Torso pointing at the camera looks short; then the outline decides.
        outline_flat = box_w > box_h * 1.3
        horizontal = torso_flat or (outline_flat and dy < box_w * 0.35)
        return PoseReading(cx, cy, horizontal, True)

    # No clear torso (partly hidden). Only a clearly flat outline counts as lying.
    return PoseReading(cx, cy, box_w > box_h * 1.6, False)


class PoseEstimator(object):
    """MediaPipe Tasks Pose Landmarker around a local .task file."""

    def __init__(self, model_path: str = ""):
        self.error = ""
        self.model_path = find_model(model_path or os.environ.get("POSE_MODEL_PATH", ""))
        self._landmarker = None
        self._mp = None
        if not self.model_path:
            self.error = "No pose model file found in camera_backend/models/"
            return
        try:
            import mediapipe as mp
            options = mp.tasks.vision.PoseLandmarkerOptions(
                base_options=mp.tasks.BaseOptions(model_asset_path=self.model_path),
                running_mode=mp.tasks.vision.RunningMode.IMAGE,
                num_poses=1,
                min_pose_detection_confidence=0.55,
                min_pose_presence_confidence=0.55,
            )
            self._landmarker = mp.tasks.vision.PoseLandmarker.create_from_options(options)
            self._mp = mp
            logger.info("Pose model loaded: %s", os.path.basename(self.model_path))
        except Exception as e:          # MediaPipe not installed, or wrong model version
            self.error = "Pose engine could not start (%s)" % e.__class__.__name__
            logger.warning("%s: %s. Falling back to shape-based detection.", self.error, e)

    @property
    def ready(self) -> bool:
        return self._landmarker is not None

    @property
    def model_name(self) -> str:
        return os.path.basename(self.model_path) if self.model_path else ""

    def read(self, frame) -> Optional[PoseReading]:
        """One frame (BGR) -> PoseReading, or None when nobody is in view."""
        if not self.ready:
            return None
        h, w = frame.shape[:2]
        if w > 640:                      # smaller input = less CPU and heat, same result
            scale = 640.0 / w
            frame = cv2.resize(frame, (640, int(h * scale)))
            h, w = frame.shape[:2]
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        try:
            image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
            result = self._landmarker.detect(image)
        except Exception as e:
            logger.error("Pose inference failed: %s", e)
            return None
        if not result.pose_landmarks:
            return None
        return classify(result.pose_landmarks[0], w, h)

    def close(self):
        if self._landmarker is not None:
            try:
                self._landmarker.close()
            except Exception:
                pass
            self._landmarker = None
