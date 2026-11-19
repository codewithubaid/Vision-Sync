import logging
import threading
from uuid import uuid4

log = logging.getLogger(__name__)


class NotificationWorker:
    def __init__(self, config, database):
        self.config = config
        self.database = database
        self.stop_event = threading.Event()
        self.thread = None
        self.firebase_app = None
        self.error = None

    @property
    def ready(self):
        return self.firebase_app is not None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        if not self.config.fcm_enabled:
            self.error = "Firebase notifications have not been enabled on the server"
            return
        try:
            import firebase_admin
            self.firebase_app = firebase_admin.initialize_app(
                options={"projectId": self.config.firebase_project_id, "httpTimeout": 10},
                name="glass-camera-" + uuid4().hex,
            )
            # Resolve credentials now so setup problems appear before a test is sent.
            self.firebase_app.credential.get_credential()
        except Exception as error:
            self.error = "Firebase server credentials could not load"
            log.error("Firebase setup failed (%s)", type(error).__name__)
            if self.firebase_app:
                import firebase_admin
                firebase_admin.delete_app(self.firebase_app)
                self.firebase_app = None
            return
        self.error = None
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run, name="notification-delivery", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=15)
        if self.firebase_app and (self.thread is None or not self.thread.is_alive()):
            import firebase_admin
            firebase_admin.delete_app(self.firebase_app)
            self.firebase_app = None

    def run(self):
        from firebase_admin import messaging
        while not self.stop_event.is_set():
            job = self.database.next_delivery()
            if job is None:
                self.stop_event.wait(1)
                continue
            if job["user_id"] not in self.config.allowed_users:
                self.database.delivery_result(job, "Access removed", permanent=True)
                continue
            try:
                title = "Test notification" if job["type"] == "test_notification" else "Movement detected"
                message = messaging.Message(
                    fid=job["token"],
                    notification=messaging.Notification(title=title, body=job["camera_name"]),
                    data={"incident_id": job["incident_id"], "camera_id": job["camera_id"],
                          "type": job["type"], "timestamp": job["timestamp"]},
                    android=messaging.AndroidConfig(
                        priority="high",
                        notification=messaging.AndroidNotification(
                            channel_id="camera_alerts", tag=job["incident_id"],
                        ),
                    ),
                    apns=messaging.APNSConfig(
                        headers={"apns-priority": "10", "apns-collapse-id": job["incident_id"]},
                        payload=messaging.APNSPayload(aps=messaging.Aps(sound="default")),
                    ),
                )
                messaging.send(message, app=self.firebase_app)
                self.database.delivery_result(job)
            except messaging.UnregisteredError:
                self.database.delivery_result(job, "Device registration expired", permanent=True)
                self.database.remove_device(job["user_id"], job["token"])
            except Exception as error:
                self.database.delivery_result(job, type(error).__name__)
                log.warning("Push delivery failed (%s)", type(error).__name__)
