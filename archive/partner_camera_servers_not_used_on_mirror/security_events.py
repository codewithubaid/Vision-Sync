"""Retain basic motion candidates; these are not human-confirmed phone alerts."""

from collections import deque
from datetime import datetime, timezone
from uuid import uuid4


class SecurityEventTracker:
    """Called under the camera server's lock; timers use monotonic seconds."""

    def __init__(
        self, sustained_seconds=2.0, quiet_seconds=0.6,
        cooldown_seconds=10.0, max_events=50,
    ):
        if sustained_seconds <= 0 or quiet_seconds <= 0 or cooldown_seconds < 0:
            raise ValueError("Motion durations must be positive; cooldown may be zero.")
        if max_events < 1:
            raise ValueError("At least one event must be retained.")
        self.sustained_seconds = sustained_seconds
        self.quiet_seconds = quiet_seconds
        self.cooldown_seconds = cooldown_seconds
        self.enabled = False
        self.session_id = uuid4().hex
        self.latest_event_id = 0
        self._events = deque(maxlen=max_events)
        self._last_event_at = None
        self.reset_pending()

    def reset_pending(self):
        """Discard incomplete activity after disarming or camera loss."""
        self._started_at = None
        self._last_motion_at = None
        self._emitted = False

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be true or false")
        if enabled != self.enabled:
            self.enabled = enabled
            self.reset_pending()

    def observe(self, motion, now):
        if not self.enabled:
            return None
        # Tolerate brief gaps between moving frames, but never count a long
        # camera stall or quiet interval as continuous movement.
        if self._last_motion_at is not None:
            if now - self._last_motion_at > self.quiet_seconds:
                self.reset_pending()
        if not motion:
            return None
        if self._started_at is None:
            self._started_at = now
        self._last_motion_at = now
        if self._emitted or now - self._started_at < self.sustained_seconds:
            return None
        if self._last_event_at is not None:
            if now - self._last_event_at < self.cooldown_seconds:
                return None

        self.latest_event_id += 1
        event = {
            "session_id": self.session_id,
            "event_id": self.latest_event_id,
            "type": "motion_candidate",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "human_confirmed": False,
            "notification_sent": False,
        }
        self._events.append(event)
        self._last_event_at = now
        self._emitted = True
        return dict(event)

    def events_after(self, after_id=0):
        """Reading does not consume events; each viewer keeps its own cursor."""
        return [dict(event) for event in self._events if event["event_id"] > after_id]
