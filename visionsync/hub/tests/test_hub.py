"""
Tests for the VisionSync hub.   Run:  python3 -m unittest discover -s hub/tests -v
No network, camera or keys needed.
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import calendar_ics                      # noqa: E402
import camera_link                       # noqa: E402
import system as sysinfo                 # noqa: E402
from config import Config, load_env_file  # noqa: E402
from state import StateStore             # noqa: E402
from weather import WeatherService, shape_current   # noqa: E402
import visionsync_hub                    # noqa: E402
from visionsync_client import VisionSync  # noqa: E402


def ics(*events):
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(
        "BEGIN:VEVENT\r\n" + "\r\n".join(e) + "\r\nEND:VEVENT\r\n" for e in events) + "END:VCALENDAR\r\n"


def stamp(dt):
    return dt.strftime("%Y%m%dT%H%M%S")


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.now_dt = datetime(2026, 11, 12, 10, 0, 0)          # a Thursday
        self.now = int(time.mktime(self.now_dt.timetuple()))

    def titles(self, text, **kw):
        return [e["title"] for e in calendar_ics.upcoming(text, now=self.now, **kw)]

    def test_orders_and_drops_past_events(self):
        text = ics(["SUMMARY:Later", "DTSTART:20261113T090000", "DTEND:20261113T100000"],
                   ["SUMMARY:Past", "DTSTART:20261111T090000", "DTEND:20261111T100000"],
                   ["SUMMARY:Soon", "DTSTART:20261112T110000", "DTEND:20261112T120000"])
        self.assertEqual(self.titles(text), ["Soon", "Later"])

    def test_running_event_is_kept(self):
        text = ics(["SUMMARY:Running", "DTSTART:20261112T093000", "DTEND:20261112T103000"])
        self.assertEqual(self.titles(text), ["Running"])

    def test_utc_time_is_converted(self):
        text = ics(["SUMMARY:Utc", "DTSTART:20261112T120000Z", "DTEND:20261112T130000Z"])
        ev = calendar_ics.upcoming(text, now=self.now)[0]
        self.assertEqual(ev["start"], 1794484800)               # 2026-11-12 12:00 UTC

    def test_all_day_event(self):
        text = ics(["SUMMARY:Holiday", "DTSTART;VALUE=DATE:20261114", "DTEND;VALUE=DATE:20261115"])
        ev = calendar_ics.upcoming(text, now=self.now)[0]
        self.assertTrue(ev["all_day"])
        self.assertEqual(datetime.fromtimestamp(ev["start"]).hour, 0)
        self.assertEqual(datetime.fromtimestamp(ev["end"]).day, 14)

    def test_weekly_repeat_started_long_ago(self):
        text = ics(["SUMMARY:Class", "DTSTART:20200107T090000", "DTEND:20200107T100000",
                    "RRULE:FREQ=WEEKLY;BYDAY=TU,TH"])
        evs = calendar_ics.upcoming(text, now=self.now, limit=3)
        days = [datetime.fromtimestamp(e["start"]).strftime("%a %d") for e in evs]
        self.assertEqual(days, ["Tue 17", "Thu 19", "Tue 24"])   # Thu 12 09:00 already ended

    def test_daily_repeat_with_count_and_exdate(self):
        text = ics(["SUMMARY:Pill", "DTSTART:20261111T200000", "DURATION:PT15M",
                    "RRULE:FREQ=DAILY;COUNT=4", "EXDATE:20261113T200000"])
        days = [datetime.fromtimestamp(e["start"]).day for e in calendar_ics.upcoming(text, now=self.now)]
        self.assertEqual(days, [12, 14])

    def test_until_stops_the_repeat(self):
        text = ics(["SUMMARY:Sprint", "DTSTART:20261101T180000", "DTEND:20261101T190000",
                    "RRULE:FREQ=DAILY;UNTIL=20261113T235959"])
        days = [datetime.fromtimestamp(e["start"]).day for e in calendar_ics.upcoming(text, now=self.now)]
        self.assertEqual(days, [12, 13])

    def test_monthly_second_friday(self):
        text = ics(["SUMMARY:Review", "DTSTART:20260109T150000", "DTEND:20260109T160000",
                    "RRULE:FREQ=MONTHLY;BYDAY=2FR"])
        evs = calendar_ics.upcoming(text, now=self.now, days_ahead=40, limit=2)
        self.assertEqual([datetime.fromtimestamp(e["start"]).strftime("%d %b") for e in evs], ["13 Nov", "11 Dec"])

    def test_yearly_birthday(self):
        text = ics(["SUMMARY:Birthday", "DTSTART;VALUE=DATE:19990120", "RRULE:FREQ=YEARLY"])
        evs = calendar_ics.upcoming(text, now=self.now, days_ahead=90)
        self.assertEqual(datetime.fromtimestamp(evs[0]["start"]).strftime("%Y-%m-%d"), "2027-01-20")

    def test_moved_single_repeat_replaces_original(self):
        text = ics(["UID:a1", "SUMMARY:Standup", "DTSTART:20261110T090000", "DTEND:20261110T093000",
                    "RRULE:FREQ=DAILY;COUNT=5"],
                   ["UID:a1", "RECURRENCE-ID:20261113T090000", "SUMMARY:Standup (moved)",
                    "DTSTART:20261113T150000", "DTEND:20261113T153000"])
        got = [(datetime.fromtimestamp(e["start"]).strftime("%d %H"), e["title"])
               for e in calendar_ics.upcoming(text, now=self.now)]
        self.assertEqual(got, [("13 15", "Standup (moved)"), ("14 09", "Standup")])

    def test_cancelled_folded_and_escaped(self):
        text = ics(["SUMMARY:Gone", "STATUS:CANCELLED", "DTSTART:20261113T090000"],
                   ["SUMMARY:Lunch\\, with a very long ti", " tle", "LOCATION:Cafe\; FCCU",
                    "DTSTART:20261113T130000", "DTEND:20261113T140000",
                    "BEGIN:VALARM", "TRIGGER:-PT10M", "DTSTART:19700101T000000", "END:VALARM"])
        evs = calendar_ics.upcoming(text, now=self.now)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["title"], "Lunch, with a very long title")
        self.assertEqual(evs[0]["location"], "Cafe; FCCU")

    def test_garbage_does_not_crash(self):
        self.assertEqual(calendar_ics.upcoming("hello\nBEGIN:VEVENT\nDTSTART:nonsense\nEND:VEVENT", now=self.now), [])


class _Cfg(object):
    """Small stand-in for Config."""
    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.openweather_key = "k"
        self.city, self.country, self.units = "Lahore", "PK", "metric"
        self.weather_interval = 600
        self.calendar_url = "http://calendar.invalid/basic.ics"
        self.calendar_interval = 300
        self.calendar_max_events = 5
        self.calendar_days_ahead = 30


OWM = {"name": "Lahore", "dt": 1, "weather": [{"main": "Haze", "description": "haze", "icon": "50d"}],
       "main": {"temp": 24.3, "feels_like": 23.6, "humidity": 41, "temp_min": 17, "temp_max": 26},
       "wind": {"speed": 2.1}, "sys": {"sunrise": 1, "sunset": 2}}


class WeatherTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_shape(self):
        s = shape_current(OWM)
        self.assertEqual((s["temp"], s["humidity"], s["description"], s["icon"]), (24.3, 41, "Haze", "50d"))

    def test_network_loss_keeps_cached_data_marked_stale(self):
        online = {"up": True}

        def fetch(url):
            if not online["up"]:
                raise OSError("network down")
            return {"list": []} if "forecast" in url else OWM

        svc = WeatherService(_Cfg(self.dir), fetch=fetch)
        s = svc.refresh()
        self.assertTrue(s["ok"]); self.assertFalse(s["stale"]); self.assertEqual(s["error"], "")
        online["up"] = False
        s = svc.refresh()
        self.assertTrue(s["ok"]); self.assertTrue(s["stale"]); self.assertEqual(s["temp"], 24.3)
        # After a reboot with no internet the cache on disk is still shown, as stale.
        def down(url):
            raise OSError("network down")
        s = WeatherService(_Cfg(self.dir), fetch=down).refresh()
        self.assertTrue(s["ok"]); self.assertTrue(s["stale"])
        online["up"] = True
        self.assertFalse(svc.refresh()["stale"])

    def test_missing_key_is_reported_not_crashed(self):
        cfg = _Cfg(self.dir); cfg.openweather_key = ""
        s = WeatherService(cfg, fetch=lambda u: OWM).refresh()
        self.assertFalse(s["ok"]); self.assertFalse(s["configured"])


class CalendarServiceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_error_shows_srs_message_and_keeps_events(self):
        tomorrow = datetime.now() + timedelta(days=1)
        text = ics(["SUMMARY:Meeting", "DTSTART:" + stamp(tomorrow), "DTEND:" + stamp(tomorrow + timedelta(hours=1))])
        ok = {"v": True}

        def fetch(url):
            if not ok["v"]:
                raise OSError("down")
            return text

        svc = calendar_ics.CalendarService(_Cfg(self.dir), fetch=fetch)
        s = svc.refresh()
        self.assertEqual([e["title"] for e in s["events"]], ["Meeting"]); self.assertFalse(s["stale"])
        ok["v"] = False
        s = svc.refresh()
        self.assertEqual(s["error"], "Calendar unavailable, check connection.")
        self.assertTrue(s["stale"]); self.assertEqual(len(s["events"]), 1)

    def test_html_error_page_is_not_accepted_as_calendar(self):
        svc = calendar_ics.CalendarService(_Cfg(self.dir), fetch=lambda u: "<html>Sign in</html>")
        s = svc.refresh()
        self.assertFalse(s["ok"]); self.assertEqual(s["error"], "Calendar unavailable, check connection.")


class SystemTests(unittest.TestCase):
    def test_thermal_guard_has_hysteresis(self):
        g = sysinfo.ThermalGuard(85, 75)
        self.assertIsNone(g.feed(84.9))
        self.assertEqual(g.feed(85.0), "on")
        self.assertIsNone(g.feed(80.0)); self.assertTrue(g.throttled)     # still cooling
        self.assertEqual(g.feed(74.0), "off")
        self.assertIsNone(g.feed(None))

    def test_reads_millidegrees(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "temp")
            with open(p, "w") as f:
                f.write("52500\n")
            self.assertEqual(sysinfo.read_cpu_temp(p), 52.5)
            self.assertIsNone(sysinfo.read_cpu_temp(os.path.join(d, "missing")))
        finally:
            shutil.rmtree(d)

    def test_internet_check_is_fast_when_down(self):
        t = time.time()
        self.assertFalse(sysinfo.internet_up(["127.0.0.1:9"], timeout=1.0))
        self.assertLess(time.time() - t, 2.0)                    # NFR-P4


class CameraAlertTests(unittest.TestCase):
    NOW = 1000000

    def ev(self, kind, ago, **kw):
        e = {"id": kind + str(ago), "type": kind, "priority": "MEDIUM", "title": kind, "message": "",
             "timestamp": self.NOW - ago, "thumbnail": "x" * 5000}
        e.update(kw)
        return e

    def test_countdown_is_rebuilt_from_history(self):
        out = camera_link.derive_alerts([], [self.ev("FALL_COUNTDOWN", 10, countdown_seconds=30)], now=self.NOW)
        self.assertEqual(out[0]["type"], "FALL_COUNTDOWN")
        self.assertEqual(out[0]["deadline"], self.NOW + 20)
        self.assertNotIn("thumbnail", out[0])

    def test_recovery_cancels_countdown(self):
        out = camera_link.derive_alerts([], [self.ev("FALL_COUNTDOWN", 10), self.ev("FALL_RECOVERED", 3)], now=self.NOW)
        self.assertEqual([a["type"] for a in out], ["FALL_RECOVERED"])

    def test_confirmed_fall_wins(self):
        active = [self.ev("CAMERA_OFFLINE", 50), self.ev("FALL_DETECTED", 1)]
        out = camera_link.derive_alerts(active, [self.ev("FALL_COUNTDOWN", 31)], now=self.NOW)
        self.assertEqual([a["type"] for a in out], ["FALL_DETECTED", "CAMERA_OFFLINE"])

    def test_old_countdown_and_motionless_prompt(self):
        self.assertEqual(camera_link.derive_alerts([], [self.ev("FALL_COUNTDOWN", 300)], now=self.NOW), [])
        out = camera_link.derive_alerts([], [self.ev("MOTIONLESS_PROMPT", 5)], now=self.NOW)
        self.assertEqual((out[0]["type"], out[0]["deadline"]), ("MOTIONLESS_PROMPT", self.NOW + 10))


class ConfigTests(unittest.TestCase):
    def test_env_file(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, ".env")
            with open(p, "w") as f:
                f.write("# comment\nVS_TEST_A=hello\nexport VS_TEST_B=\"two words\"\nVS_TEST_C=5 # five\n")
            os.environ.pop("VS_TEST_A", None)
            os.environ["VS_TEST_B"] = "from-env"
            self.assertTrue(load_env_file(p))
            self.assertEqual(os.environ["VS_TEST_A"], "hello")
            self.assertEqual(os.environ["VS_TEST_B"], "from-env")       # real environment wins
            self.assertEqual(os.environ["VS_TEST_C"], "5")
        finally:
            shutil.rmtree(d)


class HubEndToEndTests(unittest.TestCase):
    """Starts the real socket server and talks to it with the teammates' client."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.environ.update({"HUB_SOCKET": os.path.join(self.dir, "hub.sock"), "HUB_CACHE_DIR": self.dir,
                           "VS_ENV_FILE": os.path.join(self.dir, "none.env"), "VOICE_CLEAR_SECONDS": "3"})
        self.cfg = Config(os.path.join(self.dir, "none.env"))
        self.cfg.socket_path = os.environ["HUB_SOCKET"]
        self.hub = visionsync_hub.Hub(self.cfg)
        self.hub.store.update("system", {"online": True})
        self.ipc = visionsync_hub.SocketServer(self.hub)
        self.ipc.start()
        self.vs = VisionSync(role="voice", socket_path=self.cfg.socket_path)

    def tearDown(self):
        self.vs.close()
        self.hub.stop()
        self.ipc.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_voice_conversation_reaches_state(self):
        self.assertTrue(self.vs.listening())
        self.assertEqual(self.hub.store.get("voice")["state"], "listening")
        self.assertTrue(self.hub.store.get("voice")["connected"])
        self.assertTrue(self.vs.thinking(heard="what is the weather"))
        self.assertTrue(self.vs.speaking("Sunny.", mode="cloud"))
        v = self.hub.store.get("voice")
        self.assertEqual((v["state"], v["heard"], v["reply"]), ("speaking", "what is the weather", "Sunny."))
        self.assertTrue(self.vs.listening())                      # follow-up clears the old text
        self.assertEqual(self.hub.store.get("voice")["reply"], "")
        self.assertTrue(self.vs.idle())

    def test_bad_messages_are_rejected_not_fatal(self):
        self.assertFalse(self.vs.voice(state="dancing"))
        self.assertFalse(self.vs.voice(mode="quantum"))
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self.cfg.socket_path)
        s.sendall(b"this is not json\n")
        self.assertIn(b"error", s.recv(1000))
        s.close()
        self.assertTrue(self.vs.listening())                      # hub still fine

    def test_offline_forces_local_mode(self):
        self.vs.set_mode("cloud")
        self.assertEqual(self.hub.store.get("ai")["mode"], "cloud")
        self.hub.store.update("system", {"online": False})
        self.hub._publish_ai_mode()
        self.assertEqual(self.hub.store.get("ai")["mode"], "local")
        time.sleep(0.25)
        self.assertIs(self.vs.online(), False)
        self.hub.store.update("system", {"online": True})
        self.hub._publish_ai_mode()
        self.assertEqual(self.hub.store.get("ai")["mode"], "cloud")

    def test_dashboard_subscriber_gets_pushes_and_voice_disconnect_is_seen(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(self.cfg.socket_path)
        f = s.makefile("rwb")
        f.write(b'{"type":"hello","role":"dashboard","subscribe":true}\n'); f.flush()
        first = json.loads(f.readline().decode())
        self.assertEqual(first["type"], "state")
        self.vs.speaking("Hello there")
        seen = ""
        for _ in range(5):
            st = json.loads(f.readline().decode())
            if st.get("type") == "state":
                seen = st["state"]["voice"]["reply"]
                if seen:
                    break
        self.assertEqual(seen, "Hello there")
        self.vs.close()
        deadline = time.time() + 3
        while time.time() < deadline and self.hub.store.get("voice")["connected"]:
            time.sleep(0.05)
        self.assertFalse(self.hub.store.get("voice")["connected"])
        s.close()

    def test_spoken_text_is_cleared_from_screen(self):
        self.hub._spawn("housekeeping", self.hub._housekeeping_loop)
        self.vs.speaking("Private answer")
        time.sleep(4.5)
        v = self.hub.store.get("voice")
        self.assertEqual((v["state"], v["reply"]), ("idle", ""))  # NFR-SEC3

    def test_client_survives_missing_hub(self):
        lonely = VisionSync(socket_path=os.path.join(self.dir, "nothing.sock"))
        self.assertFalse(lonely.listening())
        self.assertIsNone(lonely.state())
        self.assertEqual(lonely.context(), "")

    def test_context_text(self):
        self.hub.store.update("weather", {"ok": True, "city": "Lahore", "description": "Haze", "temp": 24.3,
                                          "humidity": 41, "units": "metric", "stale": False})
        self.assertIn("Weather in Lahore: Haze, 24 C, humidity 41%", self.vs.context())


class StateTests(unittest.TestCase):
    def test_only_real_changes_notify(self):
        st = StateStore()
        q = st.subscribe()
        self.assertTrue(st.update("system", {"cpu_temp": 50}))
        self.assertFalse(st.update("system", {"cpu_temp": 50}))
        self.assertEqual(q.qsize(), 1)
        v = st.snapshot()["version"]
        st.replace("alerts", [])
        self.assertEqual(st.snapshot()["version"], v)


if __name__ == "__main__":
    unittest.main()
