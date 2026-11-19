"""
assistant.py - the chat brain behind the app's Chat screen.

Two modes (matches the SRS "AI routing" idea):
  cloud : if ANTHROPIC_API_KEY is set and the internet works, a real AI model answers.
          The key lives ONLY on the mirror, never inside the phone app.
  local : no key or no internet: simple built-in answers about the mirror
          (modes, camera, alerts, temperature, Wi-Fi). Works offline.

The assistant can only ANSWER. It never changes settings; the switches in the app do that.
"""
import json
import os
import time
import urllib.request

API_URL = os.environ.get("ASSISTANT_API_URL", "https://api.anthropic.com/v1/messages")
MODEL = os.environ.get("ASSISTANT_MODEL", "claude-sonnet-5-5")
MAX_HISTORY = 10


def _onoff(v) -> str:
    return "ON" if v else "OFF"


def status_text(s: dict, alerts: list) -> str:
    temp = s.get("cpu_temperature_c")
    wifi = s.get("wifi_signal_dbm")
    lines = [
        f"Camera: {_onoff(s.get('camera_enabled', True))}",
        f"Security Mode: {_onoff(s.get('security_mode'))}",
        f"Home Alone Mode: {_onoff(s.get('home_alone_mode'))}",
        f"CPU temperature: {temp} C" if temp else "CPU temperature: unknown",
        f"Wi-Fi signal: {wifi} dBm" if wifi else "Wi-Fi signal: unknown",
        f"Active alerts: {len(alerts)}",
    ]
    for a in alerts[:5]:
        lines.append(f"  - {a.get('title', 'Alert')}: {a.get('message', '')}")
    return "\n".join(lines)


def local_answer(message: str, s: dict, alerts: list) -> str:
    m = message.lower()
    has = lambda *words: any(w in m for w in words)

    if has("hello", "hi ", "hey", "salam", "assalam") or m.strip() in ("hi", "hey"):
        return "Hello! I can tell you about the mirror: alerts, Security Mode, Home Alone Mode, the camera, temperature and Wi-Fi. What would you like to know?"
    if has("alert", "wrong", "problem", "anything", "happen", "intruder", "safe"):
        if not alerts:
            return "All clear. There are no active alerts right now."
        a = alerts[0]
        return f"There {'is' if len(alerts) == 1 else 'are'} {len(alerts)} active alert(s). Latest: {a.get('title', 'Alert')}. {a.get('message', '')} Open the Alerts tab to see the photo."
    if has("home alone"):
        return f"Home Alone Mode is {_onoff(s.get('home_alone_mode'))}. You can change it with the switch in the Alerts tab."
    if has("security"):
        return f"Security Mode is {_onoff(s.get('security_mode'))}. You can change it with the switch in the Alerts tab."
    if has("camera", "video", "stream"):
        on = s.get("camera_enabled", True)
        return f"The camera is {_onoff(on)}. " + ("Open the Live tab to watch." if on else "Tap the CAM chip at the top to turn it on.")
    if has("temp", "hot", "cpu"):
        t = s.get("cpu_temperature_c")
        return f"The mirror CPU is at {t} C." if t else "I can't read the CPU temperature on this device."
    if has("wifi", "wi-fi", "signal", "network"):
        w = s.get("wifi_signal_dbm")
        return f"Wi-Fi signal is {w} dBm." if w else "I can't read the Wi-Fi signal right now."
    if has("status", "summary", "how are you", "report"):
        return "Here is the mirror status:\n" + status_text(s, alerts)
    if has("help", "what can you"):
        return "Ask me things like: any alerts? is security mode on? is the camera on? how hot is the mirror? or say status."
    return ("I can answer questions about the mirror (alerts, modes, camera, temperature, Wi-Fi). "
            "For other questions the mirror needs an internet connection and an AI key.")


def _clean_history(history: list) -> list:
    """Anthropic needs messages that alternate user/assistant and start with user."""
    out = []
    for h in history[-MAX_HISTORY:]:
        role = h.get("role")
        text = str(h.get("text", "")).strip()[:1000]
        if role not in ("user", "assistant") or not text:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n" + text
        else:
            out.append({"role": role, "content": text})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def cloud_answer(message: str, history: list, s: dict, alerts: list) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("no API key")
    system = (
        "You are the friendly assistant of a smart mirror called Glass Connect. "
        "Answer in short, plain words. You can see this live status of the mirror:\n"
        + status_text(s, alerts)
        + "\nYou can only answer questions. You cannot change settings or call anyone; "
        "tell the user to use the switches in the app. Never say you did an action. "
        "If someone describes an emergency, tell them to call local emergency services now."
    )
    msgs = _clean_history(history)
    if msgs and msgs[-1]["role"] == "user":
        msgs[-1]["content"] += "\n" + message
    else:
        msgs.append({"role": "user", "content": message})
    body = {"model": MODEL, "max_tokens": 400, "system": system, "messages": msgs}
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    if not text:
        raise RuntimeError("empty reply")
    return text


def voice_assistant_answer(message):
    """The mirror's own assistant (voice_assistant/: Gemini online, Qwen3 offline) also answers
    the app's chat, so phone and mirror share one brain, one task list and the same reminders."""
    import json as _json
    import os as _os
    import urllib.request as _request
    url = _os.environ.get("VOICE_API_URL", "http://127.0.0.1:8000").rstrip("/") + "/api/chat"
    req = _request.Request(url, data=_json.dumps({"message": message}).encode("utf-8"),
                           headers={"Content-Type": "application/json"})
    with _request.urlopen(req, timeout=40) as resp:
        data = _json.loads(resp.read().decode("utf-8"))
    reply = str(data.get("response") or "").strip()
    if not data.get("success", True) or not reply:
        raise RuntimeError("no reply")
    mode = data.get("mode")
    return {"reply": reply, "mode": mode if mode in ("cloud", "local", "limited") else "local"}


def ask(message: str, history: list, s: dict, alerts: list) -> dict:
    message = (message or "").strip()[:1000]
    if message:
        try:
            return voice_assistant_answer(message)
        except Exception:
            pass  # the voice assistant is not running: answer here as before
    if not message:
        return {"reply": "Please type a message.", "mode": "local"}
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return {"reply": cloud_answer(message, history or [], s, alerts), "mode": "cloud"}
        except Exception:
            pass  # no internet / bad key: fall back to the local answers
    return {"reply": local_answer(message, s, alerts), "mode": "local"}
