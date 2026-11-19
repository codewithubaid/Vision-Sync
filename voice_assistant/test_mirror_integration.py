"""
Tests for the finished voice assistant. No microphone, models or internet needed:
    python3 -m unittest test_mirror_integration -v
"""
import os
import sys
import time
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["VS_ENV_FILE"] = os.path.join(HERE, "no-such.env")
os.environ["HUB_SOCKET"] = "/tmp/visionsync-test-no-hub.sock"
os.environ["VOICE_TIMINGS_FILE"] = "/tmp/visionsync-test-timings.csv"
for _name in ("sounddevice", "vosk", "faster_whisper", "pyttsx3"):
    try:
        __import__(_name)
    except Exception:
        sys.modules[_name] = types.ModuleType(_name)
sys.path.insert(0, HERE)

import numpy as np  # noqa: E402
import ai_router  # noqa: E402
import mirror_link  # noqa: E402
import mirror_skills  # noqa: E402
import voice_service  # noqa: E402


class FakeHub(object):
    def __init__(self, state=None):
        self.calls = []
        self._state = state if state is not None else {}

    def state(self, max_age=0.2):
        return self._state

    def online(self):
        return (self._state.get("system") or {}).get("online")

    def muted(self):
        return False

    def context(self):
        return "Now: test"

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {"ok": True, "said": name + " done"}
        return call


def demo_state():
    now = time.time()
    return {
        "system": {"online": True, "cpu_temp": 51.6, "throttled": False},
        "weather": {"ok": True, "city": "Lahore", "temp": 24.3, "humidity": 41, "description": "Haze",
                    "forecast": [{"temp": 22, "icon": "02d", "pop": 0}, {"temp": 17, "icon": "01n", "pop": 0}]},
        "calendar": {"ok": True, "events": [{"title": "FYP meeting", "start": now + 60, "end": now + 3600,
                                             "all_day": False}]},
        "alerts": [],
    }


class EndChatTests(unittest.TestCase):
    def test_farewells(self):
        cases = {"bye": True, "Good bye": True, "Thank you, good bye, bye, bye, bye.": True,
                 "thanks, goodbye, have a great day": False, "end chat": True, "please end the conversation": True,
                 "bye, add milk to my tasks": False, "I need to end work": False, "Never mind.": True,
                 "What time is it?": False, "Stop listening": False}
        for text, want in cases.items():
            self.assertEqual(voice_service.is_end_chat(text), want, text)


class SkillTests(unittest.TestCase):
    def setUp(self):
        mirror_skills.reset()
        self.hub = FakeHub(demo_state())
        for p in (mock.patch.object(mirror_link, "client", return_value=self.hub),
                  mock.patch.object(mirror_skills, "_scan", return_value=["Ubaid_WiFi", "PTCL-123"])):
            p.start()
            self.addCleanup(p.stop)

    def test_time_weather_calendar(self):
        self.assertTrue(mirror_skills.handle("What time is it?").startswith("It's "))
        self.assertIn("24 degrees in Lahore", mirror_skills.handle("What is the weather today?"))
        self.assertIn("FYP meeting", mirror_skills.handle("What's on my calendar today?"))

    def test_wifi_commands(self):
        self.assertEqual(mirror_skills.handle("Turn off the Wi-Fi."), "wifi_off done")
        self.assertEqual(mirror_skills.handle("Can you set up the Wi-Fi?"), "wifi_setup done")
        mirror_skills.handle("Please connect to Ubaid WiFi, this is my password: tiger 1234.")
        self.assertEqual(self.hub.calls[-1][:2], ("wifi_connect", ("Ubaid_WiFi", "tiger1234")))

    def test_wifi_password_asked_separately(self):
        self.assertIn("password for PTCL-123", mirror_skills.handle("Connect to PTCL-123"))
        mirror_skills.handle("The password is abc12345.")
        self.assertEqual(self.hub.calls[-1][:2], ("wifi_connect", ("PTCL-123", "abc12345")))

    def test_security_mode(self):
        with mock.patch.object(mirror_link, "camera", return_value={}) as cam:
            self.assertIn("phone app", mirror_skills.handle("Turn off security mode"))
            self.assertTrue(mirror_skills.handle("Turn on security mode").startswith("Security Mode is on"))
            cam.assert_called_with("/mode/security", {"enabled": True})

    def test_general_questions_go_to_the_ai(self):
        for text in ("How do I run a good meeting?", "How does Wi-Fi work?", "Connect me with my brother",
                     "What is the capital of France?", "I'm home alone tonight, any tips?"):
            self.assertIsNone(mirror_skills.handle(text), text)


class RouterTests(unittest.TestCase):
    def setUp(self):
        ai_router.reset_conversation_history()
        ai_router._cloud_blocked_until = 0.0
        for p in (mock.patch.object(ai_router, "gemini_client", True),
                  mock.patch.object(ai_router, "CLOUD_PROVIDER", "gemini"),
                  mock.patch.object(mirror_link, "client", return_value=None),
                  mock.patch.object(ai_router, "_is_tool_intent", return_value=False)):
            p.start()
            self.addCleanup(p.stop)

    def test_offline_goes_local_without_trying_cloud(self):
        with mock.patch.object(mirror_link, "online", return_value=False), \
                mock.patch.object(ai_router, "ask_gemini") as cloud, \
                mock.patch.object(ai_router, "ask_ollama", return_value="Local answer."):
            self.assertEqual(ai_router.ask_ai("Tell me a fun fact about owls"), "Local answer.")
        self.assertEqual(ai_router.LAST_ROUTE, "local")
        cloud.assert_not_called()

    def test_online_uses_cloud(self):
        with mock.patch.object(mirror_link, "online", return_value=True), \
                mock.patch.object(ai_router, "ask_gemini", return_value="Cloud answer."):
            self.assertEqual(ai_router.ask_ai("Tell me a fun fact about owls"), "Cloud answer.")
        self.assertEqual(ai_router.LAST_ROUTE, "cloud")

    def test_quota_used_up_pauses_cloud(self):
        with mock.patch.object(mirror_link, "online", return_value=True), \
                mock.patch.object(ai_router, "ask_gemini", side_effect=Exception("429 Client Error")) as cloud, \
                mock.patch.object(ai_router, "ask_ollama", return_value="Local."):
            self.assertEqual(ai_router.ask_ai("Tell me a joke"), "Local.")
            self.assertEqual(ai_router.ask_ai("Another joke"), "Local.")
        self.assertEqual(cloud.call_count, 1)

    def test_srs_failure_sentences(self):
        with mock.patch.object(mirror_link, "online", return_value=True), \
                mock.patch.object(ai_router, "ask_gemini", side_effect=Exception("timeout")), \
                mock.patch.object(ai_router, "ask_ollama", side_effect=RuntimeError("Connection refused")):
            self.assertEqual(ai_router.ask_ai("Tell me a joke"), "I am unable to process your request at this time.")
        with mock.patch.object(mirror_link, "online", return_value=False), \
                mock.patch.object(ai_router, "ask_ollama", side_effect=RuntimeError(
                    "Ollama HTTP 500: model requires more system memory than is available")):
            self.assertEqual(ai_router.ask_ai("Tell me a joke"), "AI assistant is temporarily limited.")
        self.assertEqual(ai_router.LAST_ROUTE, "limited")

    def test_everyday_sentences_reach_the_model(self):
        with mock.patch.object(mirror_link, "online", return_value=False), \
                mock.patch.object(ai_router, "ask_ollama", return_value="MODEL"):
            for text in ("I finished my homework", "What do I need to do to make tea?",
                         "Can you complete the sentence: roses are red"):
                self.assertEqual(ai_router.ask_ai(text), "MODEL", text)

    def test_gemini_tool_turn_keeps_signature_and_call_id(self):
        first = {"candidates": [{"content": {"role": "model", "parts": [
            {"functionCall": {"name": "list_reminders", "args": {}, "id": "call-1"}, "thoughtSignature": "sig-abc"}]}}]}
        second = {"candidates": [{"content": {"parts": [{"text": "You have no reminders."}]}}]}
        sent = []

        class Reply(object):
            ok, status_code = True, 200

            def __init__(self, data):
                self.data = data

            def json(self):
                return self.data

        def fake_post(url, **kwargs):
            sent.append(kwargs["json"])
            return Reply(first if len(sent) == 1 else second)

        with mock.patch.object(ai_router, "GEMINI_API_KEY", "k"), \
                mock.patch.object(ai_router.requests, "post", side_effect=fake_post):
            out = ai_router._gemini_tool_turn([{"role": "system", "content": "s"},
                                               {"role": "user", "content": "what are my reminders"}])
        self.assertEqual(out, "You have no reminders.")
        self.assertEqual(sent[1]["contents"][-2]["parts"][0]["thoughtSignature"], "sig-abc")
        self.assertEqual(sent[1]["contents"][-1]["parts"][0]["functionResponse"]["id"], "call-1")
        self.assertNotIn("temperature", sent[0]["generationConfig"])


class VoiceSessionTests(unittest.TestCase):
    def test_question_not_heard_then_bye(self):
        texts = iter(["What time is it?", "", "bye"])
        spoken = []

        class Speaker(object):
            engine, audio_started = "test", 0.0

            def speak(self, text):
                spoken.append(text)
                return True

            def chime(self):
                pass

        class Mic(object):
            stream = object()

            def open(self):
                self.stream = object()

            def close(self):
                self.stream = None

        hub = FakeHub()
        with mock.patch.object(voice_service, "record", return_value=np.zeros(1600, dtype=np.float32)), \
                mock.patch.object(voice_service, "transcribe", side_effect=lambda w, a: next(texts)), \
                mock.patch.object(voice_service, "STT_PROVIDER", "local"), \
                mock.patch.object(mirror_link, "client", return_value=None):
            why = voice_service._session(Mic(), None, hub, Speaker())
        self.assertEqual(why, "bye")
        self.assertTrue(spoken[0].startswith("It's "))
        self.assertEqual(spoken[1], voice_service.NOT_CAUGHT)
        names = [c[0] for c in hub.calls]
        for state in ("listening", "transcribing", "thinking", "speaking", "not_understood"):
            self.assertIn(state, names)


if __name__ == "__main__":
    unittest.main()
