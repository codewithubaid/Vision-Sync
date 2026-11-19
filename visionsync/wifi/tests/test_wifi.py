"""
Wi-Fi setup mode, end to end, with a pretend NetworkManager and the real hub.
Run:  python3 -m unittest discover -s wifi/tests -v
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
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "hub"))

import visionsync_wifi as vw                 # noqa: E402
import visionsync_hub                        # noqa: E402
from config import Config                    # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_for(cond, seconds=8.0):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class _NoRedirect(object):
    pass


def http(method, url, data=None, host=None):
    """Returns (status, body, location) without following redirects."""
    import http.client
    from urllib.parse import urlparse
    u = urlparse(url)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
    headers = {"Host": host} if host else {}
    body = None
    if data is not None:
        body = urlencode(data)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    c.request(method, u.path or "/", body=body, headers=headers)
    r = c.getresponse()
    out = (r.status, r.read().decode("utf-8", "replace"), r.getheader("Location"))
    c.close()
    return out


class WifiSetupTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.nm_state = os.path.join(self.dir, "nm.json")
        self.write_nm(active=[], networks=[["Ubaid's Wi-Fi", 82, "WPA2"], ["Neighbour", 40, "WPA2"],
                                           ["Cafe:Free", 55, ""], ["Ubaid's Wi-Fi", 60, "WPA2"], ["", 30, ""]],
                      known={"Ubaid's Wi-Fi": "correct-horse", "Cafe:Free": ""})
        os.environ["FAKE_NM"] = self.nm_state

        # real hub, HTTP only
        self.hub_port = free_port()
        cfg = Config(os.path.join(self.dir, "none.env"))
        cfg.socket_path = os.path.join(self.dir, "hub.sock")
        self.hub = visionsync_hub.Hub(cfg)
        self.httpd = visionsync_hub._ThreadingHTTPServer(("127.0.0.1", self.hub_port),
                                                         visionsync_hub.make_handler(self.hub, self.dir, self.dir))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

        self.portal_port = free_port()
        os.environ.update({"NMCLI": os.path.join(HERE, "fake_nmcli.py"), "WIFI_SETUP_IP": "127.0.0.1",
                           "WIFI_PORTAL_PORT": str(self.portal_port), "WIFI_GRACE_SECONDS": "1",
                           "WIFI_TICK_SECONDS": "0.2", "WIFI_SETUP_TIMEOUT": "600",
                           "HUB_URL": "http://127.0.0.1:%d" % self.hub_port,
                           "WIFI_SETUP_FLAG": os.path.join(self.dir, "flag")})
        self.cfg = vw.Settings()
        self.ctl = vw.WifiSetup(self.cfg)
        self.portal = "http://127.0.0.1:%d" % self.portal_port

    def tearDown(self):
        self.ctl.stop_event.set()
        if getattr(self, "t", None):
            self.t.join(5)
        self.ctl.stop_portal()
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_nm(self, **kw):
        st = {"active": [], "networks": [], "known": {}, "calls": []}
        if os.path.exists(self.nm_state):
            st.update(json.load(open(self.nm_state)))
        st.update(kw)
        json.dump(st, open(self.nm_state, "w"))

    def nm(self):
        return json.load(open(self.nm_state))

    def screen(self):
        return self.hub.store.get("setup")

    def start(self):
        self.t = threading.Thread(target=self.ctl.run, daemon=True)
        self.t.start()

    # -----------------------------------------------------------------
    def test_terse_parser(self):
        self.assertEqual(vw.split_terse(r"Cafe\:Free:55:"), ["Cafe:Free", "55", ""])
        self.assertEqual(vw.split_terse(r"a\\b:1"), ["a\\b", "1"])

    def test_online_mirror_is_left_alone(self):
        self.write_nm(active=[["Home", "802-11-wireless", "wlan0"]])
        self.start()
        time.sleep(1.8)
        self.assertEqual(self.ctl.mode, "normal")
        self.assertFalse(any(c[:2] == ["connection", "add"] for c in self.nm()["calls"]))
        self.assertFalse(self.screen()["active"])

    def test_cable_counts_as_online(self):
        self.write_nm(active=[["Wired", "802-3-ethernet", "eth0"]])
        self.start()
        time.sleep(1.8)
        self.assertEqual(self.ctl.mode, "normal")

    def test_full_setup_wrong_then_right_password(self):
        self.start()
        # 1. offline for the grace time -> hotspot + QR screen
        self.assertTrue(wait_for(lambda: self.screen()["active"] and self.screen()["state"] == "waiting"))
        s = self.screen()
        nm = self.nm()
        self.assertEqual(s["ssid"], "VisionSync-Setup")
        self.assertEqual(s["password"], nm["ap_psk"])                 # QR shows the real hotspot password
        self.assertRegex(s["password"], r"^\d{8}$")
        self.assertIn(["VisionSync-Setup", "802-11-wireless", "wlan0"], nm["active"])

        # 2. phone's internet check is sent to the setup page (captive portal)
        st, _, loc = http("GET", self.portal + "/generate_204", host="connectivitycheck.gstatic.com")
        self.assertEqual((st, loc), (302, "http://127.0.0.1/"))

        # 3. setup page lists networks: strongest first, no duplicates, no blanks, no own hotspot
        st, page, _ = http("GET", self.portal + "/", host="127.0.0.1")
        self.assertEqual(st, 200)
        self.assertEqual(page.count('name="ssid"'), 3)
        self.assertLess(page.index("Ubaid&#x27;s Wi-Fi"), page.index("Cafe:Free"))
        self.assertIn("open", page)

        # 4. short password is refused on the phone, nothing happens on the mirror
        st, _, loc = http("POST", self.portal + "/connect", {"ssid": "Ubaid's Wi-Fi", "password": "123"}, host="127.0.0.1")
        self.assertEqual((st, loc), (303, "/"))
        self.assertIn("8 to 63", http("GET", self.portal + "/", host="127.0.0.1")[1])
        self.ctl.message = ""

        # 5. wrong password -> "connecting" -> error message on the mirror, setup again
        st, page, _ = http("POST", self.portal + "/connect", {"ssid": "Ubaid's Wi-Fi", "password": "wrong-pass"}, host="127.0.0.1")
        self.assertEqual(st, 200)
        self.assertIn("Connecting to Ubaid&#x27;s Wi-Fi", page)
        self.assertTrue(wait_for(lambda: self.screen()["state"] == "connecting"))
        self.assertTrue(wait_for(lambda: self.screen()["state"] == "waiting" and "Could not join" in self.screen()["message"]))
        old_pw = self.screen()["password"]

        # 6. right password, typed in the manual box -> connected -> screen clears
        self.assertTrue(wait_for(lambda: self.ctl.portal is not None))
        st, _, _ = http("POST", self.portal + "/connect",
                        {"ssid": "Neighbour", "ssid_manual": "Ubaid's Wi-Fi", "password": "correct-horse"}, host="127.0.0.1")
        self.assertEqual(st, 200)
        self.assertTrue(wait_for(lambda: self.screen()["state"] == "connected", 12))
        self.assertEqual(self.screen()["network"], "Ubaid's Wi-Fi")
        nm = self.nm()
        self.assertNotIn(["VisionSync-Setup", "802-11-wireless", "wlan0"], nm["active"])   # hotspot off
        self.assertIn(["Ubaid's Wi-Fi", "802-11-wireless", "wlan0"], nm["active"])
        self.assertTrue(wait_for(lambda: not self.screen()["active"], 12))
        self.assertEqual(self.ctl.mode, "normal")
        self.assertNotEqual(old_pw, "")
        # the Wi-Fi password never reaches the hub state
        self.assertNotIn("correct-horse", json.dumps(self.hub.store.snapshot()))

    def test_open_network(self):
        self.start()
        self.assertTrue(wait_for(lambda: self.screen()["state"] == "waiting"))
        self.assertTrue(wait_for(lambda: self.ctl.portal is not None))
        http("POST", self.portal + "/connect", {"ssid": "Cafe:Free", "password": ""}, host="127.0.0.1")
        self.assertTrue(wait_for(lambda: self.screen()["state"] == "connected", 12))
        connect = [c for c in self.nm()["calls"] if "connect" in c and "wifi" in c][-1]
        self.assertNotIn("password", connect)

    def test_setup_now_flag(self):
        self.write_nm(active=[["Home", "802-11-wireless", "wlan0"]])
        self.start()
        time.sleep(0.6)
        self.assertEqual(self.ctl.mode, "normal")
        open(self.cfg.flag_file, "w").close()
        self.assertTrue(wait_for(lambda: self.ctl.mode == "setup"))
        self.assertFalse(os.path.exists(self.cfg.flag_file))

    def test_known_network_returns_during_setup(self):
        os.environ["WIFI_SETUP_TIMEOUT"] = "1"
        os.environ["WIFI_RECHECK_SECONDS"] = "3"
        self.ctl = vw.WifiSetup(vw.Settings())
        self.start()
        self.assertTrue(wait_for(lambda: self.ctl.mode == "setup"))
        # router comes back: NetworkManager reconnects by itself once the hotspot is down
        self.write_nm(known={"Home": "x"})
        def reconnect():
            for _ in range(80):
                st = self.nm()
                if not any(r[0] == "VisionSync-Setup" for r in st["active"]) and self.ctl.mode == "normal":
                    self.write_nm(active=[["Home", "802-11-wireless", "wlan0"]])
                    return
                time.sleep(0.1)
        threading.Thread(target=reconnect, daemon=True).start()
        self.assertTrue(wait_for(lambda: self.ctl.mode == "normal" and not self.screen()["active"]
                                 and self.ctl.offline_since is None, 12))
        os.environ["WIFI_SETUP_TIMEOUT"] = "600"

    def test_hotspot_failure_is_shown(self):
        self.write_nm(ap_fails=True)
        self.start()
        self.assertTrue(wait_for(lambda: self.screen()["state"] == "error"))
        self.assertEqual(self.ctl.mode, "normal")

    def test_hub_rejects_bad_setup_message(self):
        ok, err = self.hub.apply_setup({"state": "hacked"})
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()


class VoiceCommandTests(WifiSetupTests):
    """Voice assistant -> visionsync_client -> hub -> Wi-Fi service."""

    def setUp(self):
        super().setUp()
        self.ctl_port = free_port()
        os.environ["WIFI_CONTROL_PORT"] = str(self.ctl_port)
        self.ctl = vw.WifiSetup(vw.Settings())
        self.hub.cfg.wifi_control_url = "http://127.0.0.1:%d" % self.ctl_port
        self.ipc = visionsync_hub.SocketServer(self.hub)
        self.ipc.start()
        from visionsync_client import VisionSync
        self.vs = VisionSync(socket_path=self.hub.cfg.socket_path)

    def tearDown(self):
        self.vs.close()
        self.hub.stop()
        self.ipc.stop()
        super().tearDown()

    # the parent's flow tests are not repeated here
    test_full_setup_wrong_then_right_password = None
    test_open_network = test_setup_now_flag = test_known_network_returns_during_setup = None
    test_hotspot_failure_is_shown = test_online_mirror_is_left_alone = test_cable_counts_as_online = None
    test_terse_parser = test_hub_rejects_bad_setup_message = None

    def test_service_missing_is_spoken_clearly(self):
        r = self.vs.wifi_status()
        self.assertFalse(r["ok"])
        self.assertIn("not running", r["said"])

    def test_status_setup_off_on(self):
        self.write_nm(active=[["Home", "802-11-wireless", "wlan0"]])
        self.start()
        self.assertTrue(wait_for(lambda: self.vs.wifi_status().get("connected_to") == "Home"))
        r = self.vs.wifi_setup()
        self.assertTrue(r["ok"], r)
        self.assertIn("Scan the code", r["said"])
        self.assertTrue(wait_for(lambda: self.screen()["state"] == "waiting"))
        self.assertTrue(self.vs.wifi_off()["ok"])                       # also ends setup mode
        self.assertTrue(wait_for(lambda: not self.screen()["active"]))
        time.sleep(1.8)                                                  # past the 1 s grace
        self.assertEqual(self.ctl.mode, "normal")                        # off on purpose: no setup
        self.assertEqual(self.nm()["radio"], "off")
        self.assertTrue(self.vs.wifi_on()["ok"])
        self.assertEqual(self.nm()["radio"], "on")

    def test_spoken_connect_right_and_wrong(self):
        self.write_nm(active=[["Home", "802-11-wireless", "wlan0"]])
        self.start()
        time.sleep(0.5)
        self.assertFalse(self.vs.wifi_connect("Ubaid's Wi-Fi", "short")["ok"])
        r = self.vs.wifi_connect("Ubaid's Wi-Fi", "wrong-password")
        self.assertTrue(r["ok"], r)
        self.assertTrue(wait_for(lambda: self.screen()["state"] == "error", 12))
        self.assertIn("Could not join", self.screen()["message"])
        self.assertNotEqual(self.ctl.mode, "setup")                      # no hotspot jump on a voice typo
        self.assertTrue(wait_for(lambda: not self.screen()["active"], 12))
        r = self.vs.wifi_connect("Ubaid's Wi-Fi", "correct-horse")
        self.assertTrue(r["ok"], r)
        self.assertTrue(wait_for(lambda: self.screen()["state"] == "connected", 12))
        self.assertNotIn("correct-horse", json.dumps(self.hub.store.snapshot()))
