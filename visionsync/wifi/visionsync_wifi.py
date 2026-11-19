#!/usr/bin/env python3
"""
visionsync_wifi.py - Wi-Fi setup without a keyboard or mouse.

When the mirror has no network for a minute (first start in a new place, or the
home Wi-Fi changed), it switches to setup mode, like a smart plug or camera:

  1. The mirror makes its own Wi-Fi network, "VisionSync-Setup", and shows a QR
     code on its screen (through the VisionSync hub).
  2. The user scans the QR code with a phone camera. The phone joins that network
     and a setup page opens by itself (captive portal).
  3. The page lists nearby Wi-Fi networks. The user picks one and types the password.
  4. The mirror joins it, shows "Connected", and goes back to the dashboard.
     A wrong password shows an error on the mirror and setup mode starts again.

Runs as root (it changes network settings), as the service visionsync-wifi.
Uses NetworkManager's nmcli, which Ubuntu on the Jetson already has.
Standard library only.

Manual start (for a demo):   sudo python3 visionsync_wifi.py --setup-now
"""

import argparse
import html
import json
import logging
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs
from urllib.request import Request, urlopen

log = logging.getLogger("wifi")

AP_NAME = "VisionSync-Setup"          # NetworkManager profile name


def _env(name, default):
    return os.environ.get(name, default)


def _num(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


class Settings(object):
    def __init__(self):
        self.ap_ssid = _env("WIFI_SETUP_SSID", "VisionSync-Setup")
        self.ap_ip = _env("WIFI_SETUP_IP", "10.42.0.1")           # NetworkManager's default for hotspots
        self.portal_bind = _env("WIFI_PORTAL_BIND", "")            # empty = the hotspot address
        self.portal_port = int(_num("WIFI_PORTAL_PORT", 80))
        self.grace = _num("WIFI_GRACE_SECONDS", 60)               # offline this long -> setup mode
        self.setup_timeout = _num("WIFI_SETUP_TIMEOUT", 600)      # then re-check known networks
        self.recheck = _num("WIFI_RECHECK_SECONDS", 45)
        self.tick = _num("WIFI_TICK_SECONDS", 3)
        self.hub_url = _env("HUB_URL", "http://127.0.0.1:%s" % _env("HUB_PORT", "8765")).rstrip("/")
        self.flag_file = _env("WIFI_SETUP_FLAG", "/run/visionsync-wifi-setup-now")
        self.nmcli = _env("NMCLI", "nmcli")
        # Local-only port the hub uses for voice commands ("set up Wi-Fi", "turn Wi-Fi off")
        self.control_port = int(_num("WIFI_CONTROL_PORT", 8770))


# ------------------------------------------------------------------ nmcli
def split_terse(line):
    """nmcli -t output: fields joined by ':', with ':' and '\\' escaped by '\\'."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


class Nmcli(object):
    def __init__(self, exe="nmcli"):
        self.exe = exe

    def run(self, args, timeout=45):
        try:
            p = subprocess.run([self.exe] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, timeout=timeout)
            return p.returncode, p.stdout, p.stderr.strip()
        except FileNotFoundError:
            return 127, "", "nmcli not found"
        except subprocess.TimeoutExpired:
            return 124, "", "nmcli timed out"

    def wifi_device(self):
        rc, out, _ = self.run(["-t", "-f", "DEVICE,TYPE", "device"])
        for line in out.splitlines():
            f = split_terse(line)
            if len(f) >= 2 and f[1] == "wifi":
                return f[0]
        return ""

    def active(self):
        rc, out, _ = self.run(["-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"])
        rows = []
        for line in out.splitlines():
            f = split_terse(line)
            if len(f) >= 3:
                rows.append((f[0], f[1], f[2]))
        return rows

    def scan(self, dev):
        """Nearby networks, strongest first, one entry per name."""
        args = ["-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"]
        if dev:
            args += ["ifname", dev]
        rc, out, _ = self.run(args + ["--rescan", "yes"], timeout=30)
        best = {}
        for line in out.splitlines():
            f = split_terse(line)
            if len(f) < 3 or not f[0].strip():
                continue
            ssid = f[0]
            try:
                signal_ = int(f[1])
            except ValueError:
                signal_ = 0
            secure = f[2].strip() not in ("", "--")
            if ssid not in best or signal_ > best[ssid]["signal"]:
                best[ssid] = {"ssid": ssid, "signal": signal_, "secure": secure}
        nets = sorted(best.values(), key=lambda n: -n["signal"])
        return [n for n in nets if n["ssid"] != AP_NAME][:25]

    def start_ap(self, dev, ssid, password):
        self.run(["connection", "delete", AP_NAME])
        args = ["connection", "add", "type", "wifi", "con-name", AP_NAME, "autoconnect", "no",
                "ssid", ssid, "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
                "ipv4.method", "shared", "ipv6.method", "disabled",
                "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password,
                "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp"]
        if dev:
            args[4:4] = ["ifname", dev]
        rc, _, err = self.run(args)
        if rc != 0:
            return False, err
        rc, _, err = self.run(["connection", "up", AP_NAME], timeout=40)
        return rc == 0, err

    def stop_ap(self):
        self.run(["connection", "down", AP_NAME], timeout=20)

    def radio(self, on):
        rc, _, err = self.run(["radio", "wifi", "on" if on else "off"])
        return rc == 0, err

    def connect(self, dev, ssid, password):
        args = ["--wait", "35", "device", "wifi", "connect", ssid]
        if dev:
            args += ["ifname", dev]
        if password:
            args += ["password", password]
        rc, out, err = self.run(args, timeout=60)
        return rc == 0, err


def online_links(rows):
    """Active Wi-Fi or cable connections, not counting our own setup hotspot."""
    return [r for r in rows if r[0] != AP_NAME and r[1] in ("802-11-wireless", "wifi", "802-3-ethernet", "ethernet")]


# ------------------------------------------------------------------ controller
class WifiSetup(object):
    def __init__(self, cfg, nm=None):
        self.cfg = cfg
        self.nm = nm or Nmcli(cfg.nmcli)
        self.dev = ""
        self.mode = "normal"            # normal | setup | connecting
        self.offline_since = None
        self.setup_since = 0.0
        self.password = ""
        self.networks = []
        self.message = ""
        self.pending = None             # (ssid, password) from the phone
        self.direct = None              # (ssid, password) from a voice command, while online
        self.radio_off = False          # Wi-Fi switched off on purpose: no setup mode
        self.force_setup = False
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.portal = None
        self._last_push = 0.0
        self._last_state = None

    # ---- hub screen
    def push(self, state, force=False):
        """Tells the hub what the mirror should show. Resent every 20 s (hub restarts)."""
        if not force and state == self._last_state and time.time() - self._last_push < 20:
            return
        self._last_state, self._last_push = state, time.time()
        try:
            req = Request(self.cfg.hub_url + "/api/setup", data=json.dumps(state).encode("utf-8"),
                          headers={"Content-Type": "application/json"})
            urlopen(req, timeout=3).close()
        except Exception as e:
            log.debug("Hub not reachable: %s", e)

    def screen(self, state="waiting", network="", message=""):
        return {"active": self.mode != "normal" or state == "connected", "state": state,
                "ssid": self.cfg.ap_ssid, "password": self.password,
                "url": "http://%s/" % self.cfg.ap_ip, "network": network, "message": message}

    # ---- phone
    def request_connect(self, ssid, password):
        with self.lock:
            if self.mode != "setup":
                return False
            self.pending = (ssid, password)
            return True

    # ---- voice / hub commands
    def command(self, action, ssid="", password=""):
        """Returns (http_code, reply dict). Called by the control server."""
        if action == "status":
            links = online_links(self.nm.active())
            return 200, {"mode": self.mode, "radio": not self.radio_off,
                         "connected_to": links[0][0] if links else "", "online": bool(links)}
        if action == "setup":
            if self.radio_off:
                self.nm.radio(True)
                self.radio_off = False
            with self.lock:
                if self.mode != "normal":
                    return 200, {"ok": True, "said": "Wi-Fi setup is already on the mirror screen."}
                self.force_setup = True
            return 200, {"ok": True, "said": "Wi-Fi setup is starting. Scan the code on the mirror with your phone."}
        if action == "connect":
            ssid = (ssid or "").strip()
            if not ssid or len(ssid.encode("utf-8")) > 32:
                return 400, {"ok": False, "said": "Which network should I join?"}
            if password and not 8 <= len(password) <= 63:
                return 400, {"ok": False, "said": "Wi-Fi passwords are 8 to 63 characters long."}
            if self.radio_off:
                self.nm.radio(True)
                self.radio_off = False
            with self.lock:
                if self.mode == "setup":
                    self.pending = (ssid, password)
                elif self.mode == "normal":
                    self.direct = (ssid, password)
                else:
                    return 409, {"ok": False, "said": "I am already connecting to a network."}
            return 200, {"ok": True, "said": "Connecting to %s. Watch the mirror." % ssid}
        if action in ("off", "on"):
            on = action == "on"
            if not on and self.mode != "normal":
                self.leave_setup()
            ok, err = self.nm.radio(on)
            if not ok:
                return 500, {"ok": False, "said": "I could not change the Wi-Fi."}
            self.radio_off = not on
            self.offline_since = None
            if not on:
                self.push({"active": False, "state": "", "ssid": "", "password": "", "url": "", "network": "",
                           "message": ""}, force=True)
            return 200, {"ok": True, "said": "Wi-Fi is %s." % ("on" if on else "off. Say 'turn Wi-Fi on' to bring it back")}
        return 404, {"ok": False, "said": "Unknown Wi-Fi command."}

    def start_control(self):
        try:
            srv = _PortalServer(("127.0.0.1", self.cfg.control_port), _control_handler(self))
        except OSError as e:
            log.error("Voice control for Wi-Fi is off (port %d: %s)", self.cfg.control_port, e)
            return None
        t = threading.Thread(target=srv.serve_forever, name="control")
        t.daemon = True
        t.start()
        return srv

    # ---- steps
    def enter_setup(self, reason):
        log.warning("Entering Wi-Fi setup mode (%s).", reason)
        self.dev = self.nm.wifi_device() or self.dev
        self.networks = self.nm.scan(self.dev)      # scan first: most cards cannot scan while hosting
        self.password = "%08d" % random.SystemRandom().randint(0, 99999999)
        ok, err = self.nm.start_ap(self.dev, self.cfg.ap_ssid, self.password)
        if not ok:
            log.error("Could not start the setup hotspot: %s", err)
            self.push({"active": True, "state": "error", "ssid": self.cfg.ap_ssid, "password": "",
                       "url": "", "network": "", "message": "This mirror's Wi-Fi cannot start a setup network."},
                      force=True)
            self.offline_since = time.time()         # try again after another grace period
            return
        with self.lock:
            self.mode = "setup"
            self.setup_since = time.time()
            self.pending = None
        self.start_portal()
        self.push(self.screen("waiting", message=self.message), force=True)
        log.info("Setup network '%s' is up. %d networks found nearby.", self.cfg.ap_ssid, len(self.networks))

    def leave_setup(self):
        self.stop_portal()
        self.nm.stop_ap()
        with self.lock:
            self.mode = "normal"
            self.pending = None

    def try_join(self, ssid, password, from_setup=True):
        with self.lock:
            self.mode = "connecting"
        self.push(self.screen("connecting", network=ssid), force=True)
        time.sleep(2.0)                      # let the phone receive the "Connecting" page first
        self.stop_portal()
        self.nm.stop_ap()
        log.info("Joining '%s'.", ssid)
        ok, err = self.nm.connect(self.dev, ssid, password)
        if ok:
            log.info("Joined '%s'.", ssid)
            with self.lock:
                self.mode = "normal"
            self.message = ""
            self.offline_since = None
            self.push(self.screen("connected", network=ssid), force=True)
            self.stop_event.wait(8)
            self.push({"active": False, "state": "", "ssid": "", "password": "", "url": "", "network": "",
                       "message": ""}, force=True)
            return
        log.warning("Could not join '%s': %s", ssid, err)
        with self.lock:
            self.mode = "normal"
        self.message = "Could not join %s. Check the password and try again." % ssid
        if from_setup:
            self.enter_setup("joining failed")
            return
        # Voice command: say so, then let NetworkManager return to the old network.
        # If nothing comes back, the normal "offline" rule opens setup mode later.
        self.push({"active": True, "state": "error", "ssid": "", "password": "", "url": "", "network": ssid,
                   "message": self.message}, force=True)
        self.stop_event.wait(8)
        self.push({"active": False, "state": "", "ssid": "", "password": "", "url": "", "network": "",
                   "message": ""}, force=True)
        self.offline_since = time.time()

    def setup_now_requested(self):
        try:
            if os.path.exists(self.cfg.flag_file):
                os.remove(self.cfg.flag_file)
                return True
        except OSError:
            pass
        return False

    def tick(self):
        now = time.time()
        if self.mode == "normal":
            with self.lock:
                job, self.direct = self.direct, None
                forced_cmd, self.force_setup = self.force_setup, False
            if self.radio_off and not forced_cmd:
                self.offline_since = None
                return
            if job:
                self.try_join(job[0], job[1], from_setup=False)
                return
            forced = self.setup_now_requested() or forced_cmd
            links = online_links(self.nm.active())
            if links and not forced:
                if self.offline_since is not None:
                    log.info("Network is back (%s).", links[0][0])
                self.offline_since = None
                self.message = ""
                return
            if self.offline_since is None:
                self.offline_since = now
            if forced:
                self.message = ""
                self.enter_setup("requested")
            elif now - self.offline_since >= self.cfg.grace:
                self.enter_setup("no network for %d s" % int(self.cfg.grace))
            return

        if self.mode == "setup":
            with self.lock:
                job, self.pending = self.pending, None
            if job:
                self.try_join(*job)
                return
            self.push(self.screen("waiting", message=self.message))
            if now - self.setup_since >= self.cfg.setup_timeout:
                # Nobody came. Maybe the old network is back: give it a chance.
                log.info("No setup after %d s: checking known networks.", int(self.cfg.setup_timeout))
                self.leave_setup()
                self.push(self.screen("checking"), force=True)
                end = time.time() + self.cfg.recheck
                while time.time() < end and not self.stop_event.is_set():
                    if online_links(self.nm.active()):
                        self.offline_since = None
                        self.push({"active": False, "state": "", "ssid": "", "password": "", "url": "",
                                   "network": "", "message": ""}, force=True)
                        return
                    self.stop_event.wait(3)
                self.enter_setup("still no network")

    def run(self):
        control = self.start_control()
        self.dev = self.nm.wifi_device()
        if not self.dev:
            log.warning("No Wi-Fi device found. Setup mode needs one; watching anyway.")
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Wi-Fi setup step failed (continuing)")
            self.stop_event.wait(self.cfg.tick)
        if self.mode != "normal":
            self.leave_setup()
        if control is not None:
            control.shutdown()
            control.server_close()

    # ---- setup page server
    def start_portal(self):
        if self.portal is not None:
            return
        bind = self.cfg.portal_bind or self.cfg.ap_ip
        for _ in range(10):                       # the hotspot address may take a moment
            try:
                srv = _PortalServer((bind, self.cfg.portal_port), _portal_handler(self))
                break
            except OSError as e:
                log.debug("Setup page not ready yet: %s", e)
                time.sleep(1)
        else:
            log.error("Could not open the setup page on %s:%d", bind, self.cfg.portal_port)
            return
        self.portal = srv
        t = threading.Thread(target=srv.serve_forever, name="portal")
        t.daemon = True
        t.start()

    def stop_portal(self):
        srv, self.portal = self.portal, None
        if srv is not None:
            srv.shutdown()
            srv.server_close()


class _PortalServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Set up your mirror</title>
<style>
 body{margin:0;font:17px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0d1117;color:#eef2f5}
 main{max-width:440px;margin:0 auto;padding:28px 20px 40px}
 h1{font-size:26px;font-weight:600;margin:0 0 6px} p{color:#a3abb1;margin:0 0 22px}
 .msg{background:#3a2a10;color:#ffb84d;padding:12px 14px;border-radius:10px;margin-bottom:18px}
 label.net{display:flex;align-items:center;gap:12px;padding:13px 14px;border:1px solid #2a3139;border-radius:10px;margin-bottom:8px}
 label.net:has(input:checked){border-color:#9bdcff;background:#13202b}
 label.net input{accent-color:#9bdcff;width:20px;height:20px} label.net span{flex:1;overflow-wrap:anywhere}
 small{color:#7b858d;white-space:nowrap} .field{margin:20px 0 8px;font-weight:600}
 input[type=text],input[type=password]{width:100%;box-sizing:border-box;padding:13px;border-radius:10px;border:1px solid #2a3139;background:#161c22;color:#eef2f5;font-size:17px}
 button{width:100%;margin-top:22px;padding:15px;border:0;border-radius:12px;background:#9bdcff;color:#06121b;font-size:18px;font-weight:600}
 .empty{color:#a3abb1;padding:10px 0}
</style></head><body><main>
<h1>Connect your mirror</h1><p>Choose your home Wi-Fi. The mirror shows when it is connected.</p>
{{message}}
<form method="post" action="/connect">
{{networks}}
<div class="field">Or type the network name</div>
<input type="text" name="ssid_manual" autocomplete="off" autocapitalize="none" placeholder="Network name">
<div class="field">Password</div>
<input type="password" name="password" autocomplete="off" placeholder="Wi-Fi password">
<button type="submit">Connect</button>
</form></main></body></html>"""

DONE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Connecting</title>
<style>body{margin:0;font:18px/1.45 system-ui,sans-serif;background:#0d1117;color:#eef2f5}
main{max-width:440px;margin:0 auto;padding:40px 22px}h1{font-size:26px;font-weight:600}p{color:#a3abb1}</style>
</head><body><main><h1>%(title)s</h1><p>%(text)s</p></main></body></html>"""


def _bars(signal_):
    return "Strong" if signal_ >= 70 else "Good" if signal_ >= 50 else "Weak"


def _portal_handler(ctl):
    class H(BaseHTTPRequestHandler):
        server_version = "VisionSync"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            if host and host != ctl.cfg.ap_ip:
                # Phones test the internet (e.g. connectivitycheck.gstatic.com). Sending them
                # here is what makes the setup page open by itself.
                return self._send(302, "", extra={"Location": "http://%s/" % ctl.cfg.ap_ip})
            if self.path.startswith("/favicon"):
                return self._send(404, "")
            rows = []
            for i, n in enumerate(ctl.networks):
                rows.append('<label class="net"><input type="radio" name="ssid" value="%s"%s><span>%s</span><small>%s%s</small></label>'
                            % (html.escape(n["ssid"], True), " checked" if i == 0 else "", html.escape(n["ssid"]),
                               _bars(n["signal"]), "" if n["secure"] else ", open"))
            nets = "\n".join(rows) or '<div class="empty">No networks found. Type the name below.</div>'
            msg = '<div class="msg">%s</div>' % html.escape(ctl.message) if ctl.message else ""
            # (str.replace, not %: the page's CSS contains "100%")
            self._send(200, PAGE.replace("{{networks}}", nets).replace("{{message}}", msg))

        def do_POST(self):
            if self.path != "/connect":
                return self._send(404, "")
            try:
                n = min(4096, int(self.headers.get("Content-Length", "0") or 0))
            except ValueError:
                n = 0
            form = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            ssid = (form.get("ssid_manual", [""])[0] or form.get("ssid", [""])[0]).strip()
            password = form.get("password", [""])[0]
            problem = ""
            if not ssid or len(ssid.encode("utf-8")) > 32:
                problem = "Choose a network or type its name."
            elif password and not 8 <= len(password) <= 63:
                problem = "Wi-Fi passwords are 8 to 63 characters long."
            if problem:
                ctl.message = problem
                return self._send(303, "", extra={"Location": "/"})
            if not ctl.request_connect(ssid, password):
                return self._send(409, DONE % {"title": "Busy", "text": "The mirror is already connecting. Look at its screen."})
            self._send(200, DONE % {
                "title": "Connecting to %s" % html.escape(ssid),
                "text": "Look at the mirror. Your phone will now leave the setup network. "
                        "If the password was wrong, the mirror says so and this setup network comes back."})
    return H


def _control_handler(ctl):
    """127.0.0.1 only. JSON in, JSON out. Used by the hub (/api/wifi)."""
    class C(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.client_address[0] != "127.0.0.1":
                return self._json(403, {"ok": False})
            try:
                n = min(4096, int(self.headers.get("Content-Length", "0") or 0))
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except (ValueError, TypeError):
                return self._json(400, {"ok": False, "said": "Bad request."})
            if not isinstance(body, dict):
                body = {}
            code, reply = ctl.command(str(body.get("action", "")), str(body.get("ssid", "")),
                                      str(body.get("password", "")))
            self._json(code, reply)
    return C


# ------------------------------------------------------------------ main
def main(argv=None):
    ap = argparse.ArgumentParser(description="VisionSync Wi-Fi setup mode")
    ap.add_argument("--setup-now", action="store_true", help="ask the running service to start setup mode")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    cfg = Settings()

    if args.setup_now:
        try:
            with open(cfg.flag_file, "w") as f:
                f.write("1")
            print("Setup mode starts within a few seconds. Watch the mirror.")
            return 0
        except OSError as e:
            print("Could not ask for setup mode (%s). Run with sudo." % e)
            return 1

    if not shutil.which(cfg.nmcli):
        log.error("nmcli (NetworkManager) is not installed. Wi-Fi setup mode is off.")
        signal.pause()            # stay up quietly so systemd does not restart us in a loop
        return 0
    ctl = WifiSetup(cfg)

    def stop(*_):
        ctl.stop_event.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("Wi-Fi setup watcher started (setup after %d s offline).", int(cfg.grace))
    ctl.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
