"""
backend/pose_detector.py - Fast Contour & Pose Heuristics for Jetson Nano
Target: Jetson Nano (Python 3)
Compliance: SRS UC-3, F3, M7, M8
"""

from typing import Dict, Any, Tuple
import cv2
import numpy as np


class PoseDetector:
    def __init__(self):
        # MOG2 background subtractor with shadow suppression
        self.subtractor = cv2.createBackgroundSubtractorMOG2(
            history=500, varThreshold=25, detectShadows=True
        )
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

    def analyze_frame(self, frame: np.ndarray) -> Dict[str, Any]:
        """
        Analyzes a 720p frame for motion and determines if a human contour
        is present and whether the posture indicates a fall.
        """
        fg_mask = self.subtractor.apply(frame)

        # Threshold out shadows (MOG2 shadow pixels are gray = 127)
        _, thresh = cv2.threshold(fg_mask, 200, 255, cv2.THRESH_BINARY)
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, self.kernel)

        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        if not contours:
            return {"motion": False, "is_human": False, "is_fallen": False}

        # Find largest continuous moving region
        largest = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(largest)

        # Noise & small pet filter
        if area < 2500:
            return {"motion": True, "is_human": False, "is_pet_or_shadow": True, "area": area}

        x, y, w, h = cv2.boundingRect(largest)
        aspect_ratio = float(w) / max(1, h)

        # Verification criteria:
        # Standing human: aspect ratio typically < 0.75, height > 100px
        # Fallen human: aspect ratio >= 1.25 (bounding box is wider than tall)
        is_human = (h > 100) or (w > 120 and h > 55)
        is_fallen = is_human and (aspect_ratio >= 1.25)

        return {
            "motion": True,
            "is_human": is_human,
            "is_fallen": is_fallen,
            "bbox": (x, y, w, h),
            "aspect_ratio": round(aspect_ratio, 2),
            "area": area,
        }
