"""The user's existing OpenCV motion algorithm, with retained episode timing."""

import cv2

from security_events import SecurityEventTracker


class MotionDetector:
    def __init__(self, config):
        self.previous_gray = None
        self.tracker = SecurityEventTracker(
            sustained_seconds=config.motion_seconds,
            quiet_seconds=config.quiet_seconds,
            cooldown_seconds=config.cooldown_seconds,
        )

    def reset(self):
        self.previous_gray = None
        self.tracker.reset_pending()

    def process(self, frame, now, armed):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)
        motion = False
        if self.previous_gray is not None:
            difference = cv2.absdiff(self.previous_gray, gray)
            _, mask = cv2.threshold(difference, 25, 255, cv2.THRESH_BINARY)
            mask = cv2.dilate(mask, None, iterations=2)
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for contour in contours:
                if cv2.contourArea(contour) < 1000:
                    continue
                motion = True
                x, y, width, height = cv2.boundingRect(contour)
                cv2.rectangle(frame, (x, y), (x + width, y + height), (0, 0, 255), 2)
        self.previous_gray = gray
        self.tracker.set_enabled(armed)
        event = self.tracker.observe(motion, now)
        label = "Movement detected" if motion else "No movement"
        cv2.putText(frame, label, (15, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                    (0, 0, 255) if motion else (0, 255, 0), 2)
        return motion, event is not None
