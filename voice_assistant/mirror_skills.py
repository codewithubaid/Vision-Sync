"""
mirror_skills.py - things the assistant does with the mirror itself, without a
language model, so they are instant and still work with no internet:

  time and date; weather and calendar from the dashboard hub;
  Wi-Fi on / off / set up (QR code) / connect / status;
  Security Mode and Home Alone Mode on, and their status; camera on / off; mute;
  mirror temperature, alerts, which AI is answering.

Switching Security Mode or Home Alone Mode OFF is refused by voice on purpose:
anyone in the room could say it. That is done in the phone app.

handle(text) returns the sentence to speak, or None to let the AI answer.
"""
import difflib
import re
import subprocess
import time

import mirror_link as ml

_pending = {"ssid": None, "at": 0.0}

_QUESTION = re.compile(r"^(?:(?:hey|ok|okay|so|and|please|vision)\s+)*"
                       r"(?:is|are|was|what|what's|whats|which|how|do|does|did|am|any|tell me)\b")
_COMMAND = re.compile(r"\b(?:turn|switch|put|enable|disable|activate|deactivate|start|stop|set)\b")
_NOT_RUNNING = "The mirror service is not running, so I can't do that right now."


def reset():
    _pending["ssid"] = None


def _norm(text):
    t = str(text or "").lower().replace("’", "'")
    t = re.sub(r"\bwi[\s-]?fi\b", "wifi", t)
    t = re.sub(r"[^a-z0-9' ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _on_off(t):
    if re.search(r"\b(?:off|disable|deactivate|stop)\b", t):
        return False
    if re.search(r"\b(?:on|enable|activate|start)\b", t):
        return True
    return None


def handle(text):
    t = _norm(text)
    if not t:
        return None
    for skill in (_wifi, _modes, _status, _clock, _weather, _calendar):
        reply = skill(t, str(text))
        if reply:
            return reply
    return None


# ------------------------------------------------------------------ Wi-Fi
def _said(reply):
    if not reply:
        return _NOT_RUNNING
    return str(reply.get("said") or ("Done." if reply.get("ok") else "That did not work."))


def _scan():
    """Network names the Wi-Fi card can see (NetworkManager)."""
    try:
        out = subprocess.run(["nmcli", "-t", "-f", "SSID", "device", "wifi", "list"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True, timeout=10).stdout
    except Exception:
        return []
    return sorted({line.replace("\\:", ":").strip() for line in out.splitlines() if line.strip()})


def _match_ssid(spoken, strict=False):
    """Speech-to-text writes "Ubaid WiFi" for the network "Ubaid_WiFi": find the real name."""
    def key(s):
        return re.sub(r"[^a-z0-9]", "", s.lower())
    wants = {key(spoken), key(re.sub(r"\s*\b(?:wi[\s-]?fi|network)$", "", spoken, flags=re.I))} - {""}
    best, score = None, 0.0
    for name in _scan():
        for want in wants:
            ratio = difflib.SequenceMatcher(None, want, key(name)).ratio()
            if ratio > score:
                best, score = name, ratio
    if best is not None and score >= (0.75 if strict else 0.6):
        return best
    return None if strict else spoken


def _clean_password(p):
    p = p.strip().strip("\"'").rstrip(".!?,").strip()
    return re.sub(r"\s+", "", p)        # "tiger 1234" was said as one word; spelled letters too


def _network_name(raw):
    m = re.search(r"\bconnect\b(?:\s+(?:me|us|the mirror|yourself|it))?\s+(?:to|with|on)\s+(.+)$", raw, re.I)
    if not m:
        return None
    name = re.split(r"\s*(?:[,;!?]|\.\s|\.$|\band\b|\bthis is\b|\bit is\b|\bit's\b|\bwith\b|\bplease\b|"
                    r"\busing\b|\b(?:the |my |its |it's )?password\b)", m.group(1), maxsplit=1, flags=re.I)[0]
    return re.sub(r"^(?:the|my|our|a)\s+", "", name.strip(" .,"), flags=re.I).strip(" .,")


def _wifi(t, raw):
    c = ml.client()
    if c is None:
        return None
    raw = re.sub(r"\bwi[\s-]?fi\b", "WiFi", raw, flags=re.I)
    pw_match = re.search(r"\bpassword\b(?:\s+is)?[\s:,]+(.+)$", raw, re.I)
    password = _clean_password(pw_match.group(1)) if pw_match else ""

    # Second step after "connect to <network>": the password.
    if _pending["ssid"] and time.time() - _pending["at"] < 90:
        bare = raw.strip(" .!?")
        if not password and " " not in bare and len(bare) >= 8:
            password = bare
        if password:
            ssid = _pending["ssid"]
            reset()
            return _said(c.wifi_connect(ssid, password))
        if re.search(r"\b(?:cancel|never ?mind|forget it)\b", t):
            reset()
            return "Okay, I won't change the Wi-Fi."

    wifi_words = bool(re.search(r"\b(?:wifi|internet|network|online|offline|password)\b", t))
    question = bool(_QUESTION.match(t))
    if not wifi_words and "connect" not in t:
        return None

    if not question and ("wifi" in t or "network" in t) and re.search(
            r"\b(?:set ?up|setup|configure|change|reset|new|different|another|other)\b", t):
        return _said(c.wifi_setup())                       # QR code on the mirror, finish on the phone

    if not question and "connect" in t:
        name = _network_name(raw)
        if name is not None:
            if _norm(name) in ("", "wifi", "the wifi", "network", "internet", "the internet", "it"):
                status = c.wifi_status() or {}
                if status.get("online") and not password:
                    return "I'm already online%s." % (" through " + status["connected_to"]
                                                      if status.get("connected_to") else "")
                if password:
                    return "Which network should I join? Say: connect to, then the network name, then the password."
                return _said(c.wifi_setup())
            ssid = _match_ssid(name, strict=not wifi_words)
            if ssid is None:
                return None
            if not password:
                _pending.update(ssid=ssid, at=time.time())
                return "What is the password for %s? Or say: set up Wi-Fi, and use your phone." % ssid
            return _said(c.wifi_connect(ssid, password))

    if not question and "wifi" in t and _COMMAND.search(t):
        want = _on_off(t)
        if want is not None:
            return _said(c.wifi_on() if want else c.wifi_off())

    if re.search(r"\b(?:is|are|am)\b.{0,25}\b(?:wifi|internet|online|offline|connected)\b|\bwifi status\b"
                 r"|\bwhich (?:wifi|network)\b", t):
        status = c.wifi_status() or {}
        if status.get("said") and "online" not in status:
            return status["said"]
        if status.get("radio") is False:
            return "Wi-Fi is off. Say: turn Wi-Fi on."
        name = status.get("connected_to") or ""
        if status.get("online"):
            return "Yes, I'm online%s." % (" through " + name if name else "")
        return "I'm not connected to the internet right now%s." % (", although I'm joined to " + name if name else "")
    return None


# ------------------------------------------------------------------ modes and camera
_MODES = (
    (r"\bsecurity mode\b", "/mode/security", "security_mode", "Security Mode",
     "I'll alert your phone if someone is detected."),
    (r"\bhome ?alone mode\b", "/mode/home_alone", "home_alone_mode", "Home Alone Mode",
     "I'll watch for falls and check that you are okay."),
)


def _modes(t, raw):
    question = bool(_QUESTION.match(t))
    for pattern, path, field, label, promise in _MODES:
        if not re.search(pattern, t):
            continue
        if question:
            st = ml.camera("/status")
            if st is None:
                return "I can't reach the camera service right now."
            return "%s is %s." % (label, "on" if st.get(field) else "off")
        want = _on_off(t)
        if want is None:
            continue
        if not want:
            return "For safety, %s can only be turned off in the phone app." % label
        if ml.camera(path, {"enabled": True}) is None:
            return "I can't reach the camera service, so %s is unchanged." % label
        return "%s is on. %s" % (label, promise)

    if re.search(r"\b(?:mute yourself|mute (?:the )?(?:assistant|voice|vision|mic|microphone)|stop listening)\b", t):
        if ml.camera("/settings/voice_mute", {"muted": True}) is None:
            return "I can't reach the camera service to save that setting."
        return "Okay, I'm muted. Unmute me in the phone app."

    if re.search(r"\bcamera\b", t):
        if question and re.search(r"\b(?:is|are) (?:the |my )?camera\b", t):
            st = ml.camera("/status")
            if st is None:
                return "I can't reach the camera service right now."
            return "The camera is %s." % ("on" if st.get("camera_enabled", True) else "paused")
        want = _on_off(t) if (not question and _COMMAND.search(t)) else None
        if want is True:
            ok = ml.camera("/camera/toggle", {"enabled": True}) is not None
            return "The camera is on." if ok else "I can't reach the camera service."
        if want is False:
            st = ml.camera("/status") or {}
            if st.get("security_mode") or st.get("home_alone_mode"):
                return ("The camera is needed while Security Mode or Home Alone Mode is on. "
                        "Turn those off in the phone app first.")
            ok = ml.camera("/camera/toggle", {"enabled": False}) is not None
            return "The camera is off." if ok else "I can't reach the camera service."
    return None


# ------------------------------------------------------------------ mirror status
def _status(t, raw):
    if re.search(r"\b(?:how hot|temperature|temp)\b", t) and re.search(
            r"\b(?:mirror|jetson|device|system|cpu|processor|board|you|your)\b", t):
        system = (ml.state() or {}).get("system") or {}
        if system.get("cpu_temp") is None:
            return "I can't read my temperature right now."
        reply = "My processor is at %d degrees." % round(system["cpu_temp"])
        return reply + (" I'm cooling down, so some animations are paused." if system.get("throttled") else "")
    if re.search(r"\b(?:any|are there) (?:alerts?|alarms?|intruders?)\b|\bis everything (?:ok|okay|fine|alright|safe)\b"
                 r"|\bis (?:the|my) (?:house|home) safe\b", t):
        st = ml.state()
        if st is None:
            return "I can't reach the mirror service right now."
        alerts = st.get("alerts") or []
        if not alerts:
            return "All clear. There are no alerts."
        return "There %s %d alert%s. The latest is %s." % (
            "is" if len(alerts) == 1 else "are", len(alerts), "" if len(alerts) == 1 else "s",
            alerts[0].get("title") or "an alert")
    if re.search(r"\b(?:are you|which (?:ai|model|brain))\b.*\b(?:online|offline|cloud|local|model|ai|using)\b", t):
        return ("I'm answering with the cloud model." if ml.online()
                else "I'm offline, so I'm answering with the model on this mirror.")
    return None


# ------------------------------------------------------------------ time, weather, calendar
def _clock(t, raw):
    if re.search(r"\bwhat(?:'s| is)? the time\b|\bwhat time is it\b|\btell me the time\b|\bcurrent time\b", t) \
            and not re.search(r"\btime (?:is it )?in\b", t):
        return "It's %s." % time.strftime("%I:%M %p").lstrip("0")
    if re.search(r"\bwhat(?:'s| is)? (?:the |today's )?date\b|\bwhat day is (?:it|today)\b|\btoday's date\b", t):
        return "Today is %s." % time.strftime("%A, %d %B %Y").replace(" 0", " ")
    return None


_WEATHER = re.compile(r"\b(?:weather|forecast|how (?:hot|cold|warm) is it|is it (?:hot|cold|raining|going to rain)|"
                      r"will it rain|going to rain|temperature (?:outside|today|now)|what's the temperature|"
                      r"what is the temperature|humidity)\b")


def _weather(t, raw):
    if not _WEATHER.search(t):
        return None
    st = ml.state()
    if st is None:
        return "I can't reach the mirror's weather service right now."
    w = st.get("weather") or {}
    city = str(w.get("city") or "")
    other = re.search(r"\b(?:in|for|at) ([a-z]+(?: [a-z]+)?)$", t)
    if other and other.group(1) not in (city.lower(), "today", "tomorrow", "tonight", "the morning",
                                        "the afternoon", "the evening", "now", "the moment"):
        return None                      # another city: the dashboard only has ours
    if not w.get("ok"):
        if w.get("configured") is False:
            return "Weather is not set up yet. Add the OpenWeatherMap key to the mirror settings."
        return "I can't get the weather right now."
    reply = "It's %d degrees in %s, %s, humidity %d percent." % (
        round(w.get("temp") or 0), city or "your city", str(w.get("description") or "clear").lower(),
        round(w.get("humidity") or 0))
    forecast = (w.get("forecast") or [])[:4]
    if forecast:
        temps = [f.get("temp") or 0 for f in forecast]
        reply += " Over the next twelve hours, %d to %d degrees." % (round(min(temps)), round(max(temps)))
        if any((f.get("pop") or 0) >= 0.5 or str(f.get("icon", ""))[:2] in ("09", "10", "11") for f in forecast):
            reply += " Rain is likely, so take an umbrella."
        elif "rain" in t:
            reply += " No rain is expected."
    if "tomorrow" in t:
        reply = "I only have the next twelve hours. " + reply
    if w.get("stale"):
        reply += " This is from %s, because the mirror is offline." % time.strftime(
            "%I:%M %p", time.localtime(w.get("fetched_at") or time.time())).lstrip("0")
    return reply


_CALENDAR = re.compile(r"\b(?:my|today's|tomorrow's|next|upcoming|any) (?:calendar|schedule|events?|meetings?|"
                       r"appointments?|classes)\b|\bon my (?:calendar|schedule)\b|\bwhat(?:'s| is) next\b"
                       r"|\bwhat do i have (?:on |planned )?(?:today|tomorrow|tonight|this (?:morning|afternoon|evening))\b")


def _when(e):
    lt = time.localtime(e.get("start") or 0)
    if lt[:3] == time.localtime()[:3]:
        day = "today"
    elif lt[:3] == time.localtime(time.time() + 86400)[:3]:
        day = "tomorrow"
    else:
        day = time.strftime("%A", lt)
    if e.get("all_day"):
        return day + ", all day"
    return "%s at %s" % (day, time.strftime("%I:%M %p", lt).lstrip("0"))


def _calendar(t, raw):
    if not _CALENDAR.search(t):
        return None
    st = ml.state()
    if st is None:
        return "I can't reach the mirror's calendar right now."
    cal = st.get("calendar") or {}
    if cal.get("configured") is False:
        return "Your calendar is not connected yet. Add the Google Calendar address to the mirror settings."
    now = time.time()
    events = [e for e in cal.get("events") or [] if (e.get("end") or e.get("start") or 0) >= now]
    if cal.get("error") and not events:
        return "Calendar unavailable, check connection."          # SRS UC-1 alternate 2
    empty = "Your calendar is clear."
    if "tomorrow" in t:
        day = time.localtime(now + 86400)[:3]
        events = [e for e in events if time.localtime(e.get("start") or 0)[:3] == day]
        empty = "Nothing on your calendar tomorrow."
    elif re.search(r"\b(?:today|tonight|this (?:morning|afternoon|evening))\b", t):
        day = time.localtime(now)[:3]
        events = [e for e in events if time.localtime(e.get("start") or 0)[:3] <= day]
        empty = "Nothing else on your calendar today."
    if not events:
        return empty
    reply = "Coming up: %s." % "; ".join("%s, %s" % (_when(e), e.get("title") or "an event") for e in events[:3])
    if cal.get("stale"):
        reply += " The mirror is offline, so this may be out of date."
    return reply
