import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from config import Config  # noqa: E402
from visionsync_hub import Hub  # noqa: E402


class AgendaTest(unittest.TestCase):
    def test_tasks_and_reminders_reach_the_screen(self):
        hub = Hub(Config())
        ok, _ = hub.apply_agenda({"tasks": ["Buy milk"] * 12,
                                  "reminders": [{"title": "Take medicine", "due_at": "2026-11-15T21:00"}]})
        self.assertTrue(ok)
        agenda = hub.store.get("agenda")
        self.assertEqual(len(agenda["tasks"]), 8)
        self.assertEqual(agenda["reminders"][0]["title"], "Take medicine")
        self.assertFalse(hub.apply_agenda({"tasks": "not a list"})[0])

    def test_follow_up_listening_keeps_the_answer(self):
        hub = Hub(Config())
        hub.apply_voice({"state": "speaking", "heard": "Hi", "reply": "Hello"})
        hub.apply_voice({"state": "listening", "keep": True})
        self.assertEqual(hub.store.get("voice")["reply"], "Hello")
        hub.apply_voice({"state": "listening"})
        self.assertEqual(hub.store.get("voice")["reply"], "")


if __name__ == "__main__":
    unittest.main()
