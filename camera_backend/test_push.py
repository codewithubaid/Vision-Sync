"""Tests for push.py against a pretend Firebase. Run:  python3 -m unittest test_push"""
import base64
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import push
from security_events import SecurityEventManager, EventType


class FakeFirebase(object):
    """Answers the two addresses push.py calls: the token exchange and the send."""

    def __init__(self):
        self.sent, self.assertions, self.fail_with, self.gone = [], [], None, set()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/token":
                    outer.assertions.append(body.decode().split("assertion=")[1])
                    return self._out(200, {"access_token": "tok-123", "expires_in": 3600})
                if outer.fail_with:
                    return self._out(outer.fail_with, {"error": {"status": "UNAVAILABLE"}})
                msg = json.loads(body)["message"]
                if msg["token"] in outer.gone:
                    return self._out(404, {"error": {"status": "NOT_FOUND", "details": [{"errorCode": "UNREGISTERED"}]}})
                outer.sent.append((self.headers["Authorization"], msg))
                self._out(200, {"name": "projects/demo/messages/1"})

            def _out(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def make_key_file(folder):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    path = os.path.join(folder, "sa.json")
    with open(path, "w") as f:
        json.dump({"project_id": "demo", "client_email": "mirror@demo.iam.gserviceaccount.com", "private_key": pem}, f)
    return path, key.public_key()


def wait_for(cond, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class PushTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.fb = FakeFirebase()
        push.TOKEN_URL = self.fb.base + "/token"
        push.FCM_URL = self.fb.base + "/v1/projects/%s/messages:send"
        self.key_path, self.public_key = make_key_file(self.dir)
        self.sender = push.PushSender(self.key_path, os.path.join(self.dir, "devices.json"))
        self.sender.start()

    def tearDown(self):
        self.sender.stop()
        self.fb.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_alert_reaches_every_registered_phone(self):
        self.sender.register("phone-A")
        self.sender.register("phone-B")
        self.sender.notify_event({"type": EventType.FALL_DETECTED, "id": "fall_1", "message": "No recovery.", "timestamp": 5})
        self.assertTrue(wait_for(lambda: len(self.fb.sent) == 2))
        auth, msg = self.fb.sent[0]
        self.assertEqual(auth, "Bearer tok-123")
        self.assertEqual(msg["notification"]["title"], "FALL DETECTED - immediate attention required")
        self.assertEqual(msg["data"], {"type": "FALL_DETECTED", "id": "fall_1", "timestamp": "5"})
        self.assertEqual(msg["android"]["priority"], "high")
        self.assertEqual(len(self.fb.assertions), 1)                  # token fetched once, then reused

    def test_signature_is_valid_rs256(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        self.sender.register("phone-A")
        self.sender.enqueue("t", "b")
        self.assertTrue(wait_for(lambda: self.fb.sent))
        h, c, sig = self.fb.assertions[0].split(".")
        pad = lambda x: base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))
        self.public_key.verify(pad(sig), (h + "." + c).encode(), padding.PKCS1v15(), hashes.SHA256())   # raises if wrong
        claims = json.loads(pad(c))
        self.assertEqual(claims["iss"], "mirror@demo.iam.gserviceaccount.com")
        self.assertEqual(claims["scope"], push.SCOPE)

    def test_countdown_and_minor_events_are_not_pushed(self):
        self.sender.register("phone-A")
        for kind in (EventType.FALL_COUNTDOWN, EventType.FALL_RECOVERED, EventType.ALL_CLEAR, EventType.CALL_ATTEMPT):
            self.sender.notify_event({"type": kind, "id": "x", "message": "m"})
        time.sleep(0.6)
        self.assertEqual(self.fb.sent, [])

    def test_offline_alert_is_kept_and_sent_when_internet_returns(self):
        self.sender.register("phone-A")
        self.fb.fail_with = 503                                        # "no internet"
        self.sender.notify_event({"type": EventType.SECURITY_ALERT, "id": "sec_1", "message": "Person."})
        time.sleep(0.8)
        self.assertEqual(self.fb.sent, [])
        self.assertEqual(self.sender.queued, 1)                        # still waiting (UC-3 A4)
        self.fb.fail_with = None
        self.sender._wake.set()                                        # (the worker also retries every 10 s)
        self.assertTrue(wait_for(lambda: len(self.fb.sent) == 1))
        self.assertEqual(self.sender.queued, 0)

    def test_expired_phone_is_removed_and_others_still_get_it(self):
        self.sender.register("old-phone")
        self.sender.register("phone-B")
        self.fb.gone.add("old-phone")
        self.sender.enqueue("t", "b")
        self.assertTrue(wait_for(lambda: len(self.fb.sent) == 1))
        self.assertEqual(self.fb.sent[0][1]["token"], "phone-B")
        self.assertEqual(self.sender.devices, ["phone-B"])

    def test_phone_list_survives_restart_and_can_unregister(self):
        self.sender.register("phone-A")
        again = push.PushSender(self.key_path, os.path.join(self.dir, "devices.json"))
        self.assertEqual(again.devices, ["phone-A"])
        self.assertEqual(again.register("phone-A", enabled=False), 0)
        with self.assertRaises(ValueError):
            again.register("has space")

    def test_not_configured_is_off_not_broken(self):
        off = push.PushSender("", os.path.join(self.dir, "d2.json"))
        self.assertFalse(off.ready)
        self.assertIn("FIREBASE_SERVICE_ACCOUNT", off.error)
        off.notify_event({"type": EventType.FALL_DETECTED, "id": "1", "message": "m"})   # must not raise
        missing = push.PushSender(os.path.join(self.dir, "nope.json"), os.path.join(self.dir, "d3.json"))
        self.assertIn("not found", missing.error)

    def test_security_engine_events_flow_into_push(self):
        self.sender.register("phone-A")
        mgr = SecurityEventManager(log_path=os.path.join(self.dir, "e.log"), use_pose=False)
        mgr.on_event = self.sender.notify_event
        mgr.report_camera_offline("unplugged")
        self.assertTrue(wait_for(lambda: len(self.fb.sent) == 1))
        self.assertEqual(self.fb.sent[0][1]["notification"]["title"], "Camera offline - monitoring interrupted")


if __name__ == "__main__":
    unittest.main()
