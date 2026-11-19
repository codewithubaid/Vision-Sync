"""
test_security_events.py - Unit Tests & Verification Harness
Tests SRS UC-3, F3, NFR-P3, and Edge Cases
"""

import time
import unittest
import numpy as np
from security_events import SecurityEventManager, EventType, EventPriority


class TestSecurityEvents(unittest.TestCase):
    def setUp(self):
        import tempfile, os
        self._dir = tempfile.mkdtemp()
        self._log = os.path.join(self._dir, "test_events.log")
        self.manager = SecurityEventManager(log_path=self._log, use_pose=False)
        self.manager.start()

    def tearDown(self):
        self.manager.stop()
        import shutil
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_security_mode_toggle(self):
        self.assertFalse(self.manager.security_mode)
        self.manager.set_security_mode(True)
        self.assertTrue(self.manager.security_mode)
        self.manager.set_security_mode(False)
        self.assertFalse(self.manager.security_mode)

    def test_home_alone_mode_toggle(self):
        self.assertFalse(self.manager.home_alone_mode)
        self.manager.set_home_alone_mode(True)
        self.assertTrue(self.manager.home_alone_mode)

    def test_quiet_case_pet_and_shadow_filtered(self):
        self.manager.set_security_mode(True)
        blank = np.zeros((720, 1280, 3), dtype=np.uint8)
        pose = self.manager._estimate_pose_and_verify_human(blank, (10, 10, 20, 20))
        self.assertFalse(pose["is_human"])
        self.assertEqual(len(self.manager.get_active_alerts()), 0)

    def test_security_alert_triggers_with_human(self):
        self.manager.set_security_mode(True)
        dummy_frame = np.full((720, 1280, 3), 120, dtype=np.uint8)
        thumb = self.manager._frame_to_thumbnail_b64(dummy_frame)
        self.manager._trigger_security_alert(thumb)

        active = self.manager.get_active_alerts()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["type"], EventType.SECURITY_ALERT)
        self.assertEqual(active[0]["priority"], EventPriority.HIGH)
        self.assertIn("thumbnail", active[0])

    def test_fall_countdown_and_recovery(self):
        self.manager.set_home_alone_mode(True)
        dummy_frame = np.full((720, 1280, 3), 120, dtype=np.uint8)
        thumb = self.manager._frame_to_thumbnail_b64(dummy_frame)

        fallen_pose = {"is_human": True, "is_fallen": True, "confidence": 0.9, "bbox": [100, 400, 300, 80]}
        self.manager._evaluate_home_alone_rules(fallen_pose, thumb)

        self.assertEqual(self.manager.fall_state, "COUNTDOWN_30S")
        self.assertEqual(len(self.manager.get_active_alerts()), 0)

        upright_pose = {"is_human": True, "is_fallen": False, "confidence": 0.9, "bbox": [100, 200, 100, 280]}
        self.manager._evaluate_home_alone_rules(upright_pose, thumb)

        self.assertEqual(self.manager.fall_state, "IDLE")
        history = self.manager.get_event_history(limit=5)
        types = [e["type"] for e in history]
        self.assertIn(EventType.FALL_RECOVERED, types)

    def test_camera_offline_critical_alert(self):
        self.manager.report_camera_offline("UVC device disconnected")
        active = self.manager.get_active_alerts()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["type"], EventType.CAMERA_OFFLINE)
        self.assertEqual(active[0]["priority"], EventPriority.CRITICAL)

    def test_dismiss_alert(self):
        self.manager.report_camera_offline("Sensor blocked")
        self.assertEqual(len(self.manager.get_active_alerts()), 1)

        self.manager.dismiss_alert(None)
        self.assertEqual(len(self.manager.get_active_alerts()), 0)

    # ---- Added tests (bug fixes) ----
    def test_enabling_home_alone_does_not_trigger_instant_prompt(self):
        self.manager.last_motion_time = time.time() - 3600  # nobody moved for an hour
        self.manager.set_home_alone_mode(True)
        time.sleep(1.2)  # let the monitor loop run
        types = [e["type"] for e in self.manager.get_event_history(20)]
        self.assertNotIn(EventType.MOTIONLESS_PROMPT, types)

    def test_camera_off_pauses_motionless_checks(self):
        self.manager.set_home_alone_mode(True)
        self.manager.set_detection_paused(True)
        self.manager.last_motion_time = time.time() - 3600
        time.sleep(1.2)
        types = [e["type"] for e in self.manager.get_event_history(20)]
        self.assertNotIn(EventType.MOTIONLESS_PROMPT, types)

    def test_motionless_prompt_then_escalation(self):
        self.manager.set_home_alone_mode(True)
        self.manager.last_motion_time = time.time() - 61
        time.sleep(1.2)
        types = [e["type"] for e in self.manager.get_event_history(20)]
        self.assertIn(EventType.MOTIONLESS_PROMPT, types)
        self.manager.motionless_prompt_time = time.time() - 16  # grace period over
        time.sleep(1.2)
        kinds = [a["type"] for a in self.manager.get_active_alerts()]
        self.assertIn(EventType.MOTIONLESS_ESCALATION, kinds)

    def test_history_is_capped(self):
        self.manager.max_history = 20
        for i in range(60):
            self.manager._append_event_log({"id": str(i), "type": EventType.ALL_CLEAR, "timestamp": i})
        self.assertEqual(len(self.manager.get_event_history(100)), 20)

    def test_quiet_events_are_rate_limited_and_not_in_history(self):
        for _ in range(10):
            self.manager._log_quiet_event(EventType.PET_SHADOW_FILTERED, "x")
        self.assertEqual(len(self.manager.get_event_history(100)), 0)

    def test_security_alert_cooldown(self):
        self.manager.set_security_mode(True)
        self.manager._trigger_security_alert("t")
        self.manager._trigger_security_alert("t")
        self.assertEqual(len(self.manager.get_active_alerts()), 1)

    def test_call_attempt_logged_with_masked_phone(self):
        self.manager.log_call_attempt("PRIMARY", "+923001234567", 1, "dialed")
        ev = self.manager.get_event_history(5)[-1]
        self.assertEqual(ev["type"], EventType.CALL_ATTEMPT)
        self.assertNotIn("3001234567", ev["message"])
        self.assertIn("567", ev["message"])



# ---------------------------------------------------------------- merged version: pose path
from pose_fall import PoseReading, classify


class FakePose(object):
    """Stands in for the MediaPipe estimator: returns whatever the test queued."""
    ready = True
    model_name = "fake.task"

    def __init__(self):
        self.next = None

    def read(self, frame):
        return self.next

    def close(self):
        pass


def upright(x=0.5, y=0.5):
    return PoseReading(x, y, False)


def lying(x=0.5, y=0.8):
    return PoseReading(x, y, True)


class TestPoseHomeAlone(unittest.TestCase):
    def setUp(self):
        import tempfile, os
        self._dir = tempfile.mkdtemp()
        self.manager = SecurityEventManager(log_path=os.path.join(self._dir, "e.log"), pose_estimator=FakePose())
        self.manager._play_speaker_prompt = lambda message: None      # no espeak during tests
        self.manager.start()
        self.manager.set_home_alone_mode(True)
        self.t = time.time()

    def tearDown(self):
        self.manager.stop()
        import shutil
        shutil.rmtree(self._dir, ignore_errors=True)

    def feed(self, reading, dt=0.25):
        self.t += dt
        self.manager.observe_pose(reading, self.t)

    def types(self):
        return [e["type"] for e in self.manager.get_event_history(50)]

    def test_reports_pose_detector(self):
        self.assertEqual(self.manager.fall_detector, "pose")
        self.assertEqual(SecurityEventManager(log_path=self.manager.log_path, use_pose=False).fall_detector, "shape")

    def test_fall_starts_countdown_and_standing_up_cancels_it(self):
        for _ in range(4):
            self.feed(upright())
        self.feed(lying())
        self.assertEqual(self.manager.fall_state, "IDLE")             # one frame is not enough
        self.feed(lying())
        self.assertEqual(self.manager.fall_state, "COUNTDOWN_30S")
        self.assertIn(EventType.FALL_COUNTDOWN, self.types())
        self.feed(None)                                               # body hidden for a moment
        self.assertEqual(self.manager.fall_state, "COUNTDOWN_30S")     # hidden is not recovered
        self.feed(upright()); self.feed(upright())
        self.assertEqual(self.manager.fall_state, "IDLE")
        self.assertIn(EventType.FALL_RECOVERED, self.types())
        self.assertEqual(self.manager.get_active_alerts(), [])

    def test_no_recovery_becomes_emergency(self):
        self.manager.fall_countdown_seconds = 1.0
        self.feed(upright()); self.feed(upright()); self.feed(lying()); self.feed(lying())
        time.sleep(1.8)
        kinds = [a["type"] for a in self.manager.get_active_alerts()]
        self.assertIn(EventType.FALL_DETECTED, kinds)

    def test_lying_down_for_a_long_time_is_not_a_fall(self):
        self.feed(upright()); self.feed(upright())
        self.feed(None, dt=10)                    # walked out of view
        for _ in range(6):
            self.feed(lying())                    # later seen lying on the sofa
        self.assertEqual(self.manager.fall_state, "IDLE")
        self.assertNotIn(EventType.FALL_COUNTDOWN, self.types())

    def test_empty_room_never_asks_are_you_okay(self):
        self.manager.motionless_seconds = 1.0
        self.manager.person_gone_seconds = 0.5
        time.sleep(2.5)                            # nobody was ever seen
        self.assertNotIn(EventType.MOTIONLESS_PROMPT, self.types())

    def test_still_person_in_view_is_asked(self):
        self.manager.motionless_seconds = 1.0
        end = time.time() + 2.6
        while time.time() < end:                   # same spot, upright, not moving
            self.manager.observe_pose(upright(0.5, 0.5), time.time())
            time.sleep(0.2)
        self.assertIn(EventType.MOTIONLESS_PROMPT, self.types())

    def test_moving_person_is_not_asked(self):
        self.manager.motionless_seconds = 1.0
        end, x = time.time() + 2.6, 0.2
        while time.time() < end:
            x += 0.05
            self.manager.observe_pose(upright(x % 0.9, 0.5), time.time())
            time.sleep(0.2)
        self.assertNotIn(EventType.MOTIONLESS_PROMPT, self.types())

    def test_camera_offline_alert_clears_when_camera_returns(self):
        self.manager.report_camera_offline("unplugged")
        self.manager.report_camera_offline("unplugged")               # no duplicates
        self.assertEqual(len(self.manager.get_active_alerts()), 1)
        self.manager.report_camera_online()
        self.assertEqual(self.manager.get_active_alerts(), [])
        self.assertIn(EventType.CAMERA_ONLINE, self.types())

    def test_log_file_has_no_pictures(self):
        self.manager._trigger_security_alert("A" * 5000)
        with open(self.manager.log_path) as f:
            self.assertNotIn("AAAA", f.read())
        self.assertIn("thumbnail", self.manager.get_active_alerts()[0])   # app still gets it


class TestPoseSecurity(unittest.TestCase):
    def setUp(self):
        import tempfile, os
        self._dir = tempfile.mkdtemp()
        self.pose = FakePose()
        self.manager = SecurityEventManager(log_path=os.path.join(self._dir, "e.log"), pose_estimator=self.pose)
        self.manager.set_security_mode(True)
        self.frame = np.zeros((720, 1280, 3), dtype=np.uint8)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_person_confirmed_by_pose_raises_alert(self):
        self.pose.next = upright()
        self.manager._process_with_pose(self.frame, time.time(), True, 9000)
        self.assertEqual(self.manager.get_active_alerts()[0]["title"], "Security Alert: Person Detected")

    def test_small_movement_without_a_body_is_filtered(self):
        self.pose.next = None
        self.manager._process_with_pose(self.frame, time.time(), True, 2000)
        self.assertEqual(self.manager.get_active_alerts(), [])

    def test_sustained_large_movement_without_a_body_still_alerts(self):
        self.pose.next = None
        t = time.time()
        self.manager._process_with_pose(self.frame, t, True, 9000)
        self.assertEqual(self.manager.get_active_alerts(), [])
        self.manager._process_with_pose(self.frame, t + 6, True, 9000)
        a = self.manager.get_active_alerts()
        self.assertEqual(len(a), 1)
        self.assertIn("could not be confirmed", a[0]["message"])


class _P(object):
    def __init__(self, x, y, v=0.9):
        self.x, self.y, self.visibility = x, y, v


def _body(points):
    pts = [_P(0.5, 0.5, 0.0) for _ in range(33)]
    for i, (x, y) in points.items():
        pts[i] = _P(x, y)
    return pts


class TestClassify(unittest.TestCase):
    STAND = {0: (.5, .15), 11: (.46, .3), 12: (.54, .3), 23: (.47, .55), 24: (.53, .55), 27: (.47, .9), 28: (.53, .9)}
    LIE = {0: (.2, .8), 11: (.3, .78), 12: (.3, .84), 23: (.5, .79), 24: (.5, .85), 27: (.75, .8), 28: (.75, .86)}

    def test_standing_and_lying(self):
        self.assertFalse(classify(_body(self.STAND), 1280, 720).horizontal)
        self.assertTrue(classify(_body(self.LIE), 1280, 720).horizontal)

    def test_wide_frame_does_not_fake_a_fall(self):
        # Leaning: shoulders 0.10 of the width left of the hips, 0.25 of the height above.
        lean = {0: (.38, .12), 11: (.38, .3), 12: (.44, .3), 23: (.49, .55), 24: (.53, .55), 27: (.5, .9), 28: (.54, .9)}
        self.assertFalse(classify(_body(lean), 1280, 720).horizontal)

    def test_too_few_points_is_nobody(self):
        self.assertIsNone(classify(_body({0: (.5, .5), 11: (.4, .5)}), 1280, 720))


class TestAssistant(unittest.TestCase):
    S = {"camera_enabled": True, "security_mode": True, "home_alone_mode": False,
         "cpu_temperature_c": 61.5, "wifi_signal_dbm": -55}

    def setUp(self):
        import os
        os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_local_answers(self):
        import assistant
        r = assistant.ask("Is security mode on?", [], self.S, [])
        self.assertEqual(r["mode"], "local"); self.assertIn("ON", r["reply"])
        self.assertIn("All clear", assistant.ask("any alerts?", [], self.S, [])["reply"])
        self.assertIn("61.5", assistant.ask("how hot is it", [], self.S, [])["reply"])
        self.assertIn("1 active", assistant.ask("anything wrong?", [], self.S, [{"title": "Fall", "message": "x"}])["reply"])

    def test_empty_message(self):
        import assistant
        self.assertIn("type a message", assistant.ask("  ", [], self.S, [])["reply"])

    def test_history_cleaning(self):
        import assistant
        h = [{"role": "assistant", "text": "hi"}, {"role": "user", "text": "a"}, {"role": "user", "text": "b"},
             {"role": "assistant", "text": "c"}, {"role": "bogus", "text": "z"}]
        out = assistant._clean_history(h)
        self.assertEqual([m["role"] for m in out], ["user", "assistant"])
        self.assertEqual(out[0]["content"], "a\nb")

    def test_cloud_with_fake_server_and_fallback(self):
        import os, json, threading, assistant
        from http.server import BaseHTTPRequestHandler, HTTPServer
        seen = {}
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers["Content-Length"]); seen["body"] = json.loads(self.rfile.read(n))
                seen["key"] = self.headers.get("x-api-key")
                out = json.dumps({"content": [{"type": "text", "text": "Cloud says hi"}]}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
            def log_message(self, *a): pass
        srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        assistant.API_URL = f"http://127.0.0.1:{srv.server_port}/v1/messages"
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        r = assistant.ask("hello there", [{"role": "user", "text": "earlier"}, {"role": "assistant", "text": "ok"}], self.S, [])
        self.assertEqual(r, {"reply": "Cloud says hi", "mode": "cloud"})
        self.assertEqual(seen["key"], "test-key")
        self.assertEqual(seen["body"]["messages"][-1]["content"], "hello there")
        self.assertIn("Security Mode: ON", seen["body"]["system"])
        srv.shutdown(); srv.server_close()
        # server gone -> falls back to local instead of crashing
        r2 = assistant.ask("is the camera on", [], self.S, [])
        self.assertEqual(r2["mode"], "local")
        os.environ.pop("ANTHROPIC_API_KEY", None)


if __name__ == "__main__":
    unittest.main()
