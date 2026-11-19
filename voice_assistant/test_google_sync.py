"""
test_google_sync.py - the mirror, the phone app and Google keep the same tasks and reminders.
    python3 -m unittest -v test_google_sync      (no internet: Google is replaced by a fake)
"""
import logging
import os
import stat
import sys
import tempfile
import time
import unittest
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import google_link  # noqa: E402
import google_sync  # noqa: E402
import notes  # noqa: E402
import reminder_manager  # noqa: E402

CALLS = ("list_tasks", "add_task", "update_task", "delete_task",
         "list_events", "add_event", "move_event", "delete_event")


class FakeGoogle:
    """Google Tasks and Google Calendar, in memory."""

    def __init__(self):
        self.tasks, self.events, self.calls, self.n = {}, {}, [], 0

    def _new(self, kind):
        self.n += 1
        return "%s%d" % (kind, self.n)

    def list_tasks(self):
        return [dict(t) for t in self.tasks.values()], True

    def add_task(self, title, due=None, done=False):
        gid = self._new("t")
        self.tasks[gid] = {"id": gid, "title": title, "status": "completed" if done else "needsAction"}
        if due:
            self.tasks[gid]["due"] = due + "T00:00:00.000Z"
        self.calls.append(("add_task", gid))
        return gid

    def update_task(self, gid, title=None, done=None):
        if gid not in self.tasks:
            raise google_link.Gone("gone", 404)
        if title is not None:
            self.tasks[gid]["title"] = title
        if done is not None:
            self.tasks[gid]["status"] = "completed" if done else "needsAction"
        self.calls.append(("update_task", gid))

    def delete_task(self, gid):
        self.tasks.pop(gid, None)
        self.calls.append(("delete_task", gid))

    def list_events(self, time_min, time_max=None, mine=False):
        low = datetime.fromisoformat(time_min)
        high = datetime.fromisoformat(time_max) if time_max else None
        out = []
        for ev in self.events.values():
            start = datetime.fromisoformat(ev["start"]["dateTime"])
            end = datetime.fromisoformat(ev["end"]["dateTime"])
            ours = ev.get("extendedProperties", {}).get("private", {}).get("visionsync") == "reminder"
            if end > low and (high is None or start < high) and (ours or not mine):
                out.append(dict(ev))
        return out, True

    def _put(self, gid, title, start, minutes, ours):
        start = google_link._when(start)
        self.events[gid] = {"id": gid, "summary": title, "status": "confirmed",
                            "start": {"dateTime": start.isoformat()},
                            "end": {"dateTime": (start + timedelta(minutes=minutes)).isoformat()}}
        if ours:
            self.events[gid]["extendedProperties"] = {"private": {"visionsync": "reminder"}}

    def add_event(self, title, start, minutes=15):
        gid = self._new("e")
        self._put(gid, title, start, minutes, True)
        self.calls.append(("add_event", gid))
        return gid

    def move_event(self, gid, title, start, minutes=15):
        if gid not in self.events:
            raise google_link.Gone("gone", 404)
        ours = "extendedProperties" in self.events[gid]
        self._put(gid, title, start, minutes, ours)

    def delete_event(self, gid):
        self.events.pop(gid, None)
        self.calls.append(("delete_event", gid))

    def phone_event(self, title, start):
        """Added in Google Calendar on the phone."""
        gid = self._new("p")
        self._put(gid, title, start, 60, False)
        return gid


def setUpModule():
    warnings.simplefilter("ignore", ResourceWarning)    # notes.py leaves closing to the garbage collector
    logging.getLogger("visionsync.google").setLevel(logging.ERROR)


def soon(hours=2):
    return datetime.now().replace(second=0, microsecond=0) + timedelta(hours=hours)


class SyncCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        d = Path(folder.name)
        self.g = FakeGoogle()
        self.who = {"connected": True, "email": "owner@example.com", "sub": "1", "problem": ""}
        patches = [mock.patch.object(notes, "DATABASE", str(d / "notes.db")),
                   mock.patch.object(reminder_manager, "NOTES_DB", d / "notes.db"),
                   mock.patch.object(reminder_manager, "REMINDER_DB", d / "reminders.db"),
                   mock.patch.object(google_link, "TOKEN_FILE", d / "google_token.json"),
                   mock.patch.object(google_sync, "GRACE", -1),
                   mock.patch.object(google_sync, "_schema_for", None),
                   mock.patch.object(google_link, "connected", return_value=True),
                   mock.patch.object(google_link, "account", side_effect=lambda: dict(self.who))]
        patches += [mock.patch.object(google_link, name, getattr(self.g, name)) for name in CALLS]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        google_sync.ensure_schema()                     # what the voice assistant does when it starts

    def sync(self):
        changed = google_sync.sync_once()
        self.assertEqual(google_sync._state["problem"], "")
        return changed

    def done(self, reminder_id):
        """What POST /api/reminders/{id}/done does when the reminder is ticked in the app."""
        with google_sync._db(reminder_manager.REMINDER_DB) as c:
            c.execute("UPDATE reminders SET status = 'done' WHERE id = ?", (reminder_id,))


class TaskTests(SyncCase):
    def test_a_task_said_to_vision_reaches_google_then_done_and_delete_follow(self):
        tid = notes.add_note("buy milk")
        self.sync()
        (gid, task), = self.g.tasks.items()
        self.assertEqual(task["title"], "buy milk")
        self.assertNotIn("due", task)                   # no day was said: no due date on Google
        notes.complete_note(tid)
        self.sync()
        self.assertEqual(self.g.tasks[gid]["status"], "completed")
        notes.delete_note_by_id(tid)
        self.sync()
        self.assertEqual(self.g.tasks, {})

    def test_a_task_for_another_day_gets_that_due_date(self):
        day = (date.today() + timedelta(days=3)).isoformat()
        notes.add_note("exam", day)
        self.sync()
        self.assertEqual(list(self.g.tasks.values())[0]["due"][:10], day)

    def test_a_task_added_in_the_app_or_google_tasks_reaches_the_mirror(self):
        gid = self.g.add_task("pay fees")
        self.assertEqual(self.sync(), 1)
        self.assertEqual([(r[1], r[4]) for r in notes.get_all_notes()], [("pay fees", "pending")])
        self.g.tasks[gid]["status"] = "completed"       # ticked in the app
        self.g.tasks[gid]["title"] = "pay the fees"
        self.sync()
        self.assertEqual([(r[1], r[4]) for r in notes.get_all_notes()], [("pay the fees", "completed")])
        del self.g.tasks[gid]                           # deleted in the app
        self.sync()
        self.assertEqual(notes.get_all_notes(), [])
        self.assertNotIn(("delete_task", gid), self.g.calls)    # not sent back to Google

    def test_finished_tasks_stay_where_they_are(self):
        old = notes.add_note("finished before Google was connected")
        notes.complete_note(old)
        self.g.add_task("finished on Google", done=True)
        self.sync()
        self.assertEqual(len(self.g.tasks), 1)
        self.assertEqual([r[1] for r in notes.get_all_notes()], ["finished before Google was connected"])
        self.sync()                                      # and nothing more happens
        self.assertEqual(len(self.g.calls), 1)


class ReminderTests(SyncCase):
    def test_a_reminder_said_to_vision_becomes_a_calendar_event(self):
        r = reminder_manager.add_reminder("call mom", soon())
        self.sync()
        (eid, ev), = self.g.events.items()
        self.assertEqual(ev["summary"], "call mom")
        self.assertEqual(google_sync._minutes(ev["start"]["dateTime"]), r["due_at"])
        reminder_manager.delete_reminder(r["id"])       # deleted by voice or in the app
        self.sync()
        self.assertEqual(self.g.events, {})

    def test_moving_or_deleting_the_event_on_the_phone_changes_the_mirror(self):
        r = reminder_manager.add_reminder("dentist", soon())
        self.sync()
        eid = next(iter(self.g.events))
        later = soon(5)
        self.g.move_event(eid, "dentist at 4", later)
        self.sync()
        mine = reminder_manager.get_reminder(r["id"])
        self.assertEqual((mine["title"], mine["due_at"]), ("dentist at 4", later.isoformat(timespec="minutes")))
        del self.g.events[eid]
        self.sync()
        self.assertIsNone(reminder_manager.get_reminder(r["id"]))
        self.assertNotIn(("delete_event", eid), self.g.calls)

    def test_an_event_added_in_google_calendar_becomes_a_mirror_reminder(self):
        start = soon(3)
        eid = self.g.phone_event("team meeting", start)
        self.g.phone_event("already started", datetime.now() - timedelta(minutes=10))
        self.sync()
        (mine,) = reminder_manager.list_reminders()
        self.assertEqual((mine["title"], mine["due_at"], mine["source"]),
                         ("team meeting", start.isoformat(timespec="minutes"), "calendar"))
        reminder_manager.delete_reminder(mine["id"])    # deleted on the mirror or in the app
        self.sync()
        self.assertNotIn(eid, self.g.events)

    def test_ticking_done_removes_vision_s_own_event_but_keeps_a_calendar_one(self):
        own = reminder_manager.add_reminder("submit the report", soon())
        lecture = self.g.phone_event("lecture", soon(4))
        self.sync()
        own_event = next(k for k, e in self.g.events.items() if e["summary"] == "submit the report")
        for r in reminder_manager.list_reminders():
            self.done(r["id"])
        self.sync()
        self.assertNotIn(own_event, self.g.events)
        self.assertIn(lecture, self.g.events)
        self.assertEqual(reminder_manager.list_reminders(), [])
        self.sync()
        self.assertEqual(reminder_manager.list_reminders(), [])     # not brought back
        self.assertIsNotNone(own)


class AccountTests(SyncCase):
    def test_nothing_happens_until_google_is_connected_in_the_app(self):
        self.who.update(connected=False)
        with mock.patch.object(google_link, "connected", return_value=False):
            notes.add_note("buy milk")
            self.assertEqual(google_sync.sync_once(), 0)
        self.assertEqual(self.g.calls, [])

    def test_the_app_learns_which_items_are_on_google(self):
        tid = notes.add_note("buy milk")
        r = reminder_manager.add_reminder("call mom", soon())
        self.sync()
        s = google_sync.status()
        self.assertEqual((s["connected"], s["email"], s["problem"]), (True, "owner@example.com", ""))
        self.assertEqual(s["tasks"], {str(tid): next(iter(self.g.tasks))})
        self.assertEqual(s["reminders"], {str(r["id"]): next(iter(self.g.events))})

    def test_another_account_connected_in_the_app_replaces_the_first_ones_items(self):
        notes.add_note("buy milk")
        self.g.add_task("first person's task")
        self.g.phone_event("first person's meeting", soon(3))
        self.sync()
        other = FakeGoogle()
        other.add_task("second person's task")
        other.phone_event("second person's class", soon(5))
        self.who.update(sub="2", email="second@example.com")
        with mock.patch.multiple(google_link, **{name: getattr(other, name) for name in CALLS}):
            self.sync()
        self.assertEqual([r[1] for r in notes.get_all_notes()], ["second person's task"])
        self.assertEqual([r["title"] for r in reminder_manager.list_reminders()], ["second person's class"])
        self.assertEqual([t["title"] for t in other.tasks.values()], ["second person's task"])  # nothing copied over
        self.assertEqual(len(self.g.tasks), 2)                       # and nothing deleted at the first one
        self.assertNotIn("delete_task", [c[0] for c in self.g.calls])

    def test_linking_another_account_switches_at_once(self):
        notes.add_note("buy milk")
        self.sync()
        self.who.update(sub="2", email="second@example.com")
        with mock.patch.object(google_link, "link", return_value=dict(self.who)) as linked:
            who = google_sync.link("1//second-refresh", "second-access")
        linked.assert_called_once_with("1//second-refresh", "second-access")
        self.assertEqual(who["email"], "second@example.com")
        self.assertEqual(notes.get_all_notes(), [])              # the first person's items left the mirror
        self.assertEqual(len(self.g.tasks), 1)                   # and are still in their Google

    def test_disconnect_in_the_app_takes_that_accounts_items_off_the_mirror(self):
        notes.add_note("buy milk")
        self.g.phone_event("meeting", soon(3))
        self.sync()
        with mock.patch.object(google_link, "forget") as forget:
            google_sync.unlink()
        forget.assert_called_once()
        self.assertEqual(notes.get_all_notes(), [])
        self.assertEqual(reminder_manager.list_reminders(), [])
        self.assertEqual(len(self.g.tasks), 1)                       # still in the person's Google
        self.assertEqual(len(self.g.events), 1)


def google_replies(info=None, refresh=None):
    """Answers of Google's tokeninfo and token endpoints."""
    def reply(status, body):
        return mock.Mock(status_code=status, content=b"{}", json=lambda: body)

    def answer(method, url, **kw):
        if url == google_link.TOKENINFO_URL:
            return reply(200, info) if info is not None else reply(400, {"error": "invalid_token"})
        if url == google_link.TOKEN_URL:
            return reply(*(refresh or (200, {"access_token": "mirror-token", "expires_in": 3599})))
        return reply(410, {})
    return answer


APP_TOKEN = {"azp": "123-web.apps.googleusercontent.com", "aud": "123-web.apps.googleusercontent.com",
             "sub": "42", "email": "owner@example.com", "expires_in": "3000",
             "scope": "openid https://www.googleapis.com/auth/userinfo.email "
                      "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/tasks"}


class LinkTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for p in (mock.patch.object(google_link, "TOKEN_FILE", Path(folder.name) / "google_token.json"),
                  mock.patch.dict(os.environ, {"GOOGLE_CLIENT_SECRET": "GOCSPX-test", "GOOGLE_CLIENT_ID": ""})):
            p.start()
            self.addCleanup(p.stop)

    def link(self, info=APP_TOKEN, refresh=None):
        with mock.patch.object(google_link, "_request", side_effect=google_replies(info, refresh)):
            return google_link.link("1//refresh-from-the-app", "app-token")

    def test_the_account_connected_in_the_app_becomes_the_mirrors(self):
        who = self.link()
        self.assertEqual((who["connected"], who["email"], who["problem"]), (True, "owner@example.com", ""))
        saved = google_link._load()
        self.assertEqual(saved["client_id"], APP_TOKEN["azp"])      # read from the app's token, never typed
        self.assertEqual(saved["access_token"], "mirror-token")      # the mirror refreshed it by itself
        self.assertEqual(stat.S_IMODE(os.stat(google_link.TOKEN_FILE).st_mode), 0o600)

    def test_without_the_client_secret_it_still_works_for_the_rest_of_the_hour(self):
        with mock.patch.dict(os.environ, {"GOOGLE_CLIENT_SECRET": ""}):
            who = self.link()
            self.assertEqual(who["problem"], google_link.NO_SECRET)
            self.assertEqual(google_link.access_token(), "app-token")

    def test_tasks_and_calendar_must_be_allowed(self):
        with self.assertRaises(google_link.GoogleError) as err:
            self.link(dict(APP_TOKEN, scope="openid email https://www.googleapis.com/auth/calendar.readonly"))
        self.assertEqual(str(err.exception), google_link.NO_SCOPES)
        self.assertFalse(google_link.connected())

    def test_a_wrong_secret_is_reported_and_the_sign_in_kept(self):
        who = self.link(refresh=(401, {"error": "invalid_client"}))
        self.assertEqual((who["connected"], who["problem"]), (True, google_link.BAD_SECRET))

    def test_a_sign_in_google_ended_asks_to_connect_again(self):
        self.link()
        google_link._save(dict(google_link._load(), expiry=0))
        with mock.patch.object(google_link, "_request", side_effect=google_replies(refresh=(400, {"error": "invalid_grant"}))):
            with self.assertRaises(google_link.NotConnected):
                google_link.access_token()
        self.assertFalse(google_link.connected())
        self.assertEqual(google_link.account()["problem"], google_link.EXPIRED)

    def test_the_app_gets_a_token_with_most_of_its_hour_left(self):
        self.link()
        google_link._save(dict(google_link._load(), access_token="old", expiry=time.time() + 600))
        with mock.patch.object(google_link, "_request", side_effect=google_replies(
                refresh=(200, {"access_token": "new", "expires_in": 3599}))):
            given = google_link.app_token()
        self.assertEqual((given["access_token"], given["email"]), ("new", "owner@example.com"))
        self.assertGreater(given["expires_in"], 3300)

    def test_deleting_something_already_gone_is_fine(self):
        self.link()
        with mock.patch.object(google_link, "_request", side_effect=google_replies()):
            google_link.delete_event("e1")
            google_link.delete_task("t1")

    def test_a_reminder_time_goes_to_google_with_the_time_zone(self):
        begin, end = google_link._times(datetime(2026, 11, 19, 17, 0), 15)
        self.assertEqual(google_sync._minutes(begin["dateTime"]), "2026-11-19T17:00")
        self.assertEqual(google_sync._minutes(end["dateTime"]), "2026-11-19T17:15")


if __name__ == "__main__":
    unittest.main()
