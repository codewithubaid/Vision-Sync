"""
test_agenda.py - the phone app, the mirror and Google share one list of tasks and reminders.
    python3 -m unittest -v test_agenda      (no camera, voice assistant or internet needed)
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agenda  # noqa: E402


class AgendaTests(unittest.TestCase):
    def test_the_app_gets_the_mirror_list(self):
        replies = {
            "/api/tasks?view=open": (200, {"tasks": [
                {"id": 4, "note": "buy milk", "note_date": "2026-11-18", "status": "pending"}]}),
            "/api/reminders": (200, {"reminders": [
                {"id": 7, "title": "call mom", "due_at": "2026-11-18T17:00", "status": "pending"}]}),
            "/api/google": (200, {"connected": True, "email": "owner@example.com", "problem": "",
                                  "tasks": {"4": "gt4"}, "reminders": {"7": "ev7"}}),
        }
        with mock.patch.object(agenda, "call", side_effect=lambda method, path, body=None, **kw: replies[path]):
            data = agenda.agenda()
        self.assertEqual(data["tasks"], [{"id": 4, "title": "buy milk", "date": "2026-11-18", "done": False,
                                          "google_id": "gt4"}])
        self.assertEqual(data["reminders"], [{"id": 7, "title": "call mom", "due_at": "2026-11-18T17:00",
                                              "status": "pending", "google_id": "ev7"}])
        self.assertEqual((data["google"], data["google_email"], data["google_problem"]),
                         (True, "owner@example.com", ""))

    def test_an_older_mirror_without_google(self):
        replies = {
            "/api/tasks?view=open": (422, {}),
            "/api/tasks?view=pending": (200, {"tasks": [{"id": 4, "note": "buy milk", "status": "pending"}]}),
            "/api/reminders": (200, {"reminders": []}),
            "/api/google": (404, {}),
        }
        with mock.patch.object(agenda, "call", side_effect=lambda method, path, body=None, **kw: replies[path]):
            data = agenda.agenda()
        self.assertEqual([(t["title"], t["google_id"]) for t in data["tasks"]], [("buy milk", "")])
        self.assertFalse(data["google"])

    def test_voice_assistant_not_running(self):
        with mock.patch.object(agenda, "VOICE_API", "http://127.0.0.1:9"):
            self.assertEqual(agenda.call("GET", "/api/reminders")[0], 503)
            self.assertIsNone(agenda.agenda())

    def test_changes_in_the_app_reach_the_mirror(self):
        calls = []

        def fake(method, path, body=None, **kw):
            calls.append((method, path, body))
            return 200, {}

        with mock.patch.object(agenda, "call", side_effect=fake):
            agenda.handle_post("/agenda/reminder", {"title": " call mom ", "due_at": "2026-11-18T17:00"})
            agenda.handle_post("/agenda/reminder/delete", {"id": 7})
            agenda.handle_post("/agenda/task", {"title": "buy milk"})
            agenda.handle_post("/agenda/task", {"title": "exam", "date": "2026-11-25"})
            agenda.handle_post("/agenda/reminder/done", {"id": 7})
            agenda.handle_post("/agenda/sync", {})
            agenda.handle_post("/agenda/google/link", {"refresh_token": "1//r", "access_token": "ya29"})
            agenda.handle_post("/agenda/google/unlink", {})
            agenda.handle_post("/agenda/google/token", {})
            agenda.handle_post("/agenda/task/done", {"id": "4"})
            agenda.handle_post("/agenda/task/delete", {"id": 4})
            self.assertIsNone(agenda.handle_post("/agenda/other", {}))
        self.assertEqual(calls, [
            ("POST", "/api/reminders", {"title": "call mom", "due_at": "2026-11-18T17:00"}),
            ("DELETE", "/api/reminders/7", None),
            ("POST", "/api/tasks", {"note": "buy milk"}),
            ("POST", "/api/tasks", {"note": "exam", "note_date": "2026-11-25"}),
            ("POST", "/api/reminders/7/done", None),
            ("POST", "/api/google/sync", None),
            ("POST", "/api/google/link", {"refresh_token": "1//r", "access_token": "ya29"}),
            ("POST", "/api/google/unlink", None),
            ("POST", "/api/google/token", None),
            ("POST", "/api/tasks/4/complete", None),
            ("DELETE", "/api/tasks/4", None),
        ])


if __name__ == "__main__":
    unittest.main()
