from dataclasses import dataclass, field
import math
import os
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Settings:
    supabase_url: str = ""
    supabase_key: str = ""
    allowed_users: frozenset = field(default_factory=frozenset)
    camera_id: str = "1"
    camera_name: str = "Living Room Camera"
    camera_source: str = "1"
    camera_backend: str = "auto"
    motion_seconds: float = 2.0
    quiet_seconds: float = 0.6
    cooldown_seconds: float = 30.0
    database_path: Path = field(default_factory=lambda: Path(__file__).parent / "data" / "camera.sqlite3")
    fcm_enabled: bool = False
    firebase_project_id: str = ""

    def __post_init__(self):
        durations = (self.motion_seconds, self.quiet_seconds, self.cooldown_seconds)
        if not all(math.isfinite(value) for value in durations):
            raise ValueError("Motion durations must be finite")
        if self.motion_seconds <= 0 or self.quiet_seconds <= 0 or self.cooldown_seconds < 0:
            raise ValueError("Motion durations must be positive; cooldown may be zero")

    @property
    def auth_configured(self):
        return bool(
            self.supabase_url.startswith("https://")
            and self.supabase_key
            and self.allowed_users
            and "YOUR_USER_UUID" not in self.allowed_users
        )

    @classmethod
    def from_env(cls):
        load_dotenv(Path(__file__).parent / ".env", override=False)
        return cls(
            supabase_url=os.getenv("SUPABASE_URL", "").rstrip("/"),
            supabase_key=os.getenv("SUPABASE_PUBLISHABLE_KEY", ""),
            allowed_users=frozenset(filter(None, (
                value.strip() for value in os.getenv("CAMERA_ALLOWED_USER_IDS", "").split(",")
            ))),
            camera_id=os.getenv("CAMERA_ID", "1"),
            camera_name=os.getenv("CAMERA_NAME", "Living Room Camera"),
            camera_source=os.getenv("CAMERA_SOURCE", "1"),
            camera_backend=os.getenv("CAMERA_BACKEND", "auto"),
            motion_seconds=float(os.getenv("MOTION_SECONDS", "2")),
            quiet_seconds=float(os.getenv("MOTION_QUIET_SECONDS", "0.6")),
            cooldown_seconds=float(os.getenv("ALERT_COOLDOWN_SECONDS", "30")),
            fcm_enabled=os.getenv("FCM_ENABLED", "false").lower() == "true",
            firebase_project_id=os.getenv("FIREBASE_PROJECT_ID", ""),
        )
