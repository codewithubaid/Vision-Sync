"""
visionsync_client.py - Copy this ONE file next to your code to talk to the hub.

Voice pipeline (wake word -> STT -> LLM -> TTS) example:

    from visionsync_client import VisionSync
    vs = VisionSync(role="voice")

    vs.listening()                                  # wake word heard, chime played
    vs.thinking(heard="what is the weather today")  # STT finished
    vs.speaking("It is 24 degrees and hazy.", mode="cloud")   # TTS starts
    vs.idle()                                       # TTS finished
    vs.not_understood()                             # low STT confidence
    vs.failed()                                     # cloud AND local model failed

    if vs.muted():        # user muted the assistant in the phone app
        ...
    if vs.online():       # internet is up -> use the cloud model
        ...
    vs.wifi_setup()       # "set up Wi-Fi" -> QR code on the mirror
    vs.wifi_off(); vs.wifi_on(); vs.wifi_status()
    print(vs.context())   # weather + next events as text for the LLM prompt

Nothing here can raise: if the hub is not running every call returns False/None
and your pipeline keeps working (NFR-Q2, NFR-Q5). Standard library only.
"""

import json
import os
import socket
import threading
import time

DEFAULT_SOCKET = os.environ.get("HUB_SOCKET", "/tmp/visionsync.sock")


class VisionSync(object):
    def __init__(self, role="voice", socket_path=DEFAULT_SOCKET, timeout=1.0):
        self.role = role
        self.socket_path = socket_path
        self.timeout = timeout
        self._sock = None
        self._buf = b""
        self._lock = threading.Lock()
        self._state = None
        self._state_at = 0.0

    # ---------------- connection ----------------
    def _connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.socket_path)
        self._sock, self._buf = s, b""
        self._roundtrip({"type": "hello", "role": self.role})

    def _roundtrip(self, msg):
        self._sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
        while b"\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise OSError("hub closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        reply = json.loads(line.decode("utf-8"))
        if reply.get("type") == "state":
            self._state, self._state_at = reply.get("state"), time.time()
        return reply

    def _send(self, msg):
        """Sends one message; reconnects once if the hub was restarted."""
        with self._lock:
            for _ in range(2):
                try:
                    if self._sock is None:
                        self._connect()
                    return self._roundtrip(msg)
                except (OSError, ValueError):
                    self.close_quietly()
            return None

    def close_quietly(self):
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        self._sock = None

    def close(self):
        with self._lock:
            self.close_quietly()

    # ---------------- voice states (shown on the mirror) ----------------
    def voice(self, state=None, heard=None, reply=None, message=None, mode=None, keep=None):
        msg = {"type": "voice"}
        for k, v in (("state", state), ("heard", heard), ("reply", reply), ("message", message), ("mode", mode), ("keep", keep)):
            if v is not None:
                msg[k] = v
        r = self._send(msg)
        return bool(r and r.get("type") == "ok")

    def listening(self, keep=False):
        """Wake word detected. Call right after the chime."""
        return self.voice("listening", keep=True if keep else None)

    def transcribing(self):
        return self.voice("transcribing")

    def thinking(self, heard=None, mode=None):
        """STT finished; the LLM is working. `heard` is what the user said."""
        return self.voice("thinking", heard=heard, mode=mode)

    def speaking(self, reply, mode=None, heard=None):
        """TTS starts. `mode` is "cloud" or "local" (which model answered)."""
        return self.voice("speaking", reply=reply, mode=mode, heard=heard)

    def idle(self):
        """TTS finished / back to waiting for the wake word. Clears the text."""
        return self.voice("idle")

    def not_understood(self, message="I did not catch that. Please repeat your question."):
        return self.voice("error", message=message)

    def failed(self, message="I am unable to process your request at this time."):
        return self.voice("error", message=message, mode="limited")

    def set_mode(self, mode):
        """Tell the mirror which model is in use: "cloud", "local" or "limited"."""
        return self.voice(mode=mode)

    # ---------------- Wi-Fi (voice commands) ----------------
    # Each returns a dict with "ok" and "said": a sentence the assistant can speak back.
    # These work with no internet, so match them with simple keywords or the LOCAL
    # model: the cloud model cannot be reached when the Wi-Fi is the problem.
    def _wifi(self, **kw):
        kw["type"] = "wifi"
        r = self._send(kw)
        if not r or r.get("type") != "wifi":
            return {"ok": False, "said": "The mirror service is not running."}
        r.pop("type", None)
        return r

    def wifi_setup(self):
        """"Set up Wi-Fi": shows the QR code; the user finishes on the phone (safest)."""
        return self._wifi(action="setup")

    def wifi_connect(self, ssid, password=""):
        """"Connect to <network>, the password is ...". Spoken passwords are heard by
        everyone in the room and often misheard: prefer wifi_setup()."""
        return self._wifi(action="connect", ssid=ssid, password=password)

    def wifi_off(self):
        return self._wifi(action="off")

    def wifi_on(self):
        return self._wifi(action="on")

    def wifi_status(self):
        """{"online": bool, "connected_to": "Home", "mode": "normal", "radio": True}"""
        return self._wifi(action="status")

    # ---------------- tasks and reminders (shown on the mirror) ----------------
    def agenda(self, tasks, reminders):
        """tasks: list of text; reminders: list of {"title", "due_at" (local ISO time)}."""
        r = self._send({"type": "agenda", "tasks": list(tasks or []), "reminders": list(reminders or [])})
        return bool(r and r.get("type") == "ok")

    # ---------------- reading the mirror ----------------
    def state(self, max_age=0.2):
        """The hub's full state dict, or None when the hub is not running."""
        if self._state is None or time.time() - self._state_at > max_age:
            if self._send({"type": "get"}) is None:
                return None
        return self._state

    def online(self):
        """True/False = internet up/down as measured by the hub; None = unknown."""
        st = self.state()
        return st["system"].get("online") if st else None

    def muted(self):
        st = self.state()
        return bool(st and st["voice"].get("muted"))

    def throttled(self):
        """True when the Jetson is too hot: skip non-essential work (NFR-S1)."""
        st = self.state()
        return bool(st and st["system"].get("throttled"))

    def context(self):
        """Short text with the live weather and next events, for the LLM prompt."""
        st = self.state()
        if not st:
            return ""
        lines = [time.strftime("Now: %A %d %B %Y, %I:%M %p")]
        w = st.get("weather") or {}
        if w.get("ok"):
            unit = "C" if w.get("units", "metric") == "metric" else "F"
            lines.append("Weather in %s: %s, %s %s, humidity %s%%%s" % (
                w.get("city", ""), w.get("description", ""), _num(w.get("temp")), unit,
                w.get("humidity", "?"), " (old data, internet is down)" if w.get("stale") else ""))
        ev = (st.get("calendar") or {}).get("events") or []
        if ev:
            lines.append("Next calendar events:")
            for e in ev[:5]:
                when = time.strftime("%a %d %b" if e.get("all_day") else "%a %d %b %I:%M %p",
                                     time.localtime(e.get("start", 0)))
                lines.append("- %s: %s" % (when, e.get("title", "")))
        return "\n".join(lines)


def _num(v):
    try:
        return "%d" % round(float(v))
    except (TypeError, ValueError):
        return "?"


if __name__ == "__main__":
    # Quick manual test:  python3 visionsync_client.py
    vs = VisionSync()
    if vs.state() is None:
        print("Hub is not running (no socket at %s)" % vs.socket_path)
    else:
        print(vs.context())
        vs.listening(); time.sleep(1.5)
        vs.thinking(heard="what is the weather today"); time.sleep(1.5)
        vs.speaking("This is a test reply from visionsync_client.py.", mode="cloud"); time.sleep(4)
        vs.idle()
        print("Sent a test conversation to the mirror.")
