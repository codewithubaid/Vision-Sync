"""
push.py - Push notifications to the phone through Firebase Cloud Messaging (free).

SRS UC-3 step 5 and A4: an urgent alert is pushed to the phone even when the app
is closed, and when the internet is down it is kept and sent once it is back.

Setup (once):
  1. Firebase console > Project settings > Service accounts > "Generate new private key".
  2. Copy that .json file to the Jetson, outside any shared folder, for example
     /home/<you>/visionsync-firebase.json   (chmod 600).
  3. In visionsync/.env add:   FIREBASE_SERVICE_ACCOUNT=/home/<you>/visionsync-firebase.json
  4. Restart the camera service. /status then shows "push_ready": true.

Needs the Python package "cryptography" (usually already on Ubuntu:
python3-cryptography). Nothing else: the Firebase HTTP API is called directly.
Without the file or the package, push is simply off and everything else works.
"""

import base64
import collections
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger("Push")

TOKEN_URL = os.environ.get("FCM_TOKEN_URL", "https://oauth2.googleapis.com/token")
FCM_URL = os.environ.get("FCM_SEND_URL", "https://fcm.googleapis.com/v1/projects/%s/messages:send")
SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

# Alerts worth waking a phone for. The countdown itself is not pushed: the person
# may simply stand up again (UC-3 A2).
PUSH_TYPES = {
    "SECURITY_ALERT": "Security alert",
    "FALL_DETECTED": "FALL DETECTED - immediate attention required",
    "MOTIONLESS_ESCALATION": "No response - immediate attention required",
    "CAMERA_OFFLINE": "Camera offline - monitoring interrupted",
}
MAX_QUEUE = 100
MAX_AGE = 24 * 3600          # an alert older than a day is no longer "news"


def _b64(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


class PushSender(object):
    def __init__(self, service_account_path: str = "", devices_path: str = "push_devices.json"):
        self.account_path = service_account_path or os.environ.get("FIREBASE_SERVICE_ACCOUNT", "")
        self.devices_path = devices_path
        self.error = ""
        self.project_id = ""
        self._account = None
        self._key = None
        self._access_token = ""
        self._access_expiry = 0.0
        self._lock = threading.Lock()
        self._queue = collections.deque(maxlen=MAX_QUEUE)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self.sent_count = 0
        self.devices = self._load_devices()
        self._load_account()

    # ------------------------------------------------------------ setup
    def _load_account(self):
        if not self.account_path:
            self.error = "FIREBASE_SERVICE_ACCOUNT is not set"
            return
        try:
            with open(self.account_path, "r") as f:
                acct = json.load(f)
            from cryptography.hazmat.primitives import serialization
            self._key = serialization.load_pem_private_key(acct["private_key"].encode("utf-8"), password=None)
            self._account = acct
            self.project_id = acct["project_id"]
            self.error = ""
            logger.info("Push notifications ready for Firebase project %s", self.project_id)
        except ImportError:
            self.error = "Python package 'cryptography' is missing (sudo apt install -y python3-cryptography)"
        except (IOError, OSError):
            self.error = "Firebase key file not found: %s" % self.account_path
        except Exception as e:
            self.error = "Firebase key file could not be read (%s)" % e.__class__.__name__
        if self.error:
            logger.warning("Push notifications are off: %s", self.error)

    @property
    def ready(self) -> bool:
        return self._account is not None

    def _load_devices(self):
        try:
            with open(self.devices_path, "r") as f:
                data = json.load(f)
            return [str(t) for t in data.get("tokens", []) if t]
        except (IOError, OSError, ValueError):
            return []

    def _save_devices(self):
        try:
            tmp = self.devices_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"tokens": self.devices}, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.devices_path)
        except (IOError, OSError) as e:
            logger.error("Could not save the phone list: %s", e)

    # ------------------------------------------------------------ phones
    def register(self, token: str, enabled: bool = True) -> int:
        """Adds or removes a phone. Returns how many phones are registered."""
        token = (token or "").strip()
        if not token or len(token) > 4096 or any(c.isspace() for c in token):
            raise ValueError("token is missing or not valid")
        with self._lock:
            if enabled and token not in self.devices:
                self.devices.append(token)
                self.devices = self.devices[-20:]        # a household, not a crowd
            elif not enabled and token in self.devices:
                self.devices.remove(token)
            self._save_devices()
            return len(self.devices)

    # ------------------------------------------------------------ sending
    def notify_event(self, event: dict):
        """Called by the security engine for every logged event. Must be quick."""
        kind = str(getattr(event.get("type"), "value", event.get("type")))
        if kind not in PUSH_TYPES:
            return
        self.enqueue(PUSH_TYPES[kind], str(event.get("message", "")),
                     {"type": kind, "id": str(event.get("id", "")), "timestamp": str(event.get("timestamp", ""))})

    def enqueue(self, title: str, body: str, data: dict = None):
        self._queue.append({"title": title, "body": body, "data": data or {}, "at": time.time(), "left": None})
        self._wake.set()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="PushWorker", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3)

    @property
    def queued(self) -> int:
        return len(self._queue)

    def _run(self):
        while not self._stop.is_set():
            self._wake.wait(timeout=10)       # also retries every 10 s while something waits
            self._wake.clear()
            if not self.ready:
                self._queue.clear()           # nothing can be sent; do not pile up
                continue
            while self._queue and not self._stop.is_set():
                job = self._queue[0]
                if time.time() - job["at"] > MAX_AGE:
                    self._queue.popleft()
                    continue
                if job["left"] is None:
                    with self._lock:
                        job["left"] = list(self.devices)
                try:
                    self._deliver(job)
                except Exception as e:        # no internet, Firebase down: keep it, try later
                    logger.warning("Push not sent yet (%s). It stays queued.", e.__class__.__name__)
                    break
                self._queue.popleft()

    def _deliver(self, job: dict):
        """Sends one alert to every phone still waiting for it. Raises to retry later."""
        for token in list(job["left"]):
            status = self._send_one(token, job)
            if status == "gone":
                with self._lock:
                    if token in self.devices:
                        self.devices.remove(token)
                        self._save_devices()
                logger.info("A phone's registration expired and was removed.")
            job["left"].remove(token)
            if status == "sent":
                self.sent_count += 1

    def _send_one(self, token: str, job: dict) -> str:
        message = {"message": {
            "token": token,
            "notification": {"title": job["title"], "body": job["body"]},
            "data": {k: str(v) for k, v in job["data"].items()},
            "android": {"priority": "high", "notification": {"sound": "default"}},
            "apns": {"headers": {"apns-priority": "10"}, "payload": {"aps": {"sound": "default"}}},
        }}
        req = urllib.request.Request(
            FCM_URL % self.project_id, data=json.dumps(message).encode("utf-8"),
            headers={"Authorization": "Bearer " + self._token(), "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10).close()
            return "sent"
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code == 404 or (e.code == 400 and "INVALID_ARGUMENT" in detail) or "UNREGISTERED" in detail:
                return "gone"                 # that phone uninstalled the app or its token changed
            if e.code in (401, 403):
                self._access_token = ""       # key problem: say so clearly, then retry later
                self.error = "Firebase refused the server key (%d)" % e.code
            raise

    def _token(self) -> str:
        """OAuth access token from the service-account key (cached for about an hour)."""
        if self._access_token and time.time() < self._access_expiry - 60:
            return self._access_token
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        now = int(time.time())
        header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = _b64(json.dumps({
            "iss": self._account["client_email"], "scope": SCOPE,
            "aud": self._account.get("token_uri", "https://oauth2.googleapis.com/token"),
            "iat": now, "exp": now + 3600}).encode())
        signing_input = header + b"." + claims
        signature = self._key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        assertion = (signing_input + b"." + _b64(signature)).decode()
        body = ("grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer&assertion=" + assertion).encode()
        req = urllib.request.Request(TOKEN_URL, data=body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp = urllib.request.urlopen(req, timeout=10)
        try:
            out = json.loads(resp.read().decode("utf-8"))
        finally:
            resp.close()
        self._access_token = out["access_token"]
        self._access_expiry = time.time() + float(out.get("expires_in", 3600))
        self.error = ""
        return self._access_token
