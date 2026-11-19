"""
doctor.py - checks every part of the voice assistant and says how to fix what is missing.
    ~/.venvs/visionsync-voice/bin/python doctor.py
"""
import importlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mirror_link  # noqa: E402  (loads visionsync/.env)

problems = []


def check(ok, what, fix=""):
    print(("  OK   " if ok else "  FIX  ") + what)
    if not ok:
        problems.append(what)
        if fix:
            print("         -> " + fix)
    return ok


def http_json(url, body=None, headers=None, timeout=5):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    all_headers = {"Content-Type": "application/json"}
    all_headers.update(headers or {})
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=all_headers), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


print("VisionSync voice assistant check (Python %s, settings: %s)" % (
    sys.version.split()[0], mirror_link.ENV_FILE or "no .env found"))

for module in ("numpy", "requests", "sounddevice", "vosk", "faster_whisper", "fastapi", "uvicorn"):
    try:
        importlib.import_module(module)
        check(True, "Python package " + module)
    except Exception as e:
        check(False, "Python package %s (%s)" % (module, str(e)[:80]), "bash setup_voice.sh")

vosk_dir = os.environ.get("VOSK_MODEL_PATH") or os.path.join(HERE, "vosk-model")
check(os.path.isfile(os.path.join(vosk_dir, "am", "final.mdl")), "Wake-word model (%s)" % vosk_dir,
      "bash setup_voice.sh  (it looks in ~/Desktop/FYP_Project/Models)")

try:
    import sounddevice as sd
    check(True, "Microphone: " + sd.query_devices(kind="input")["name"])
except Exception as e:
    check(False, "Microphone (%s)" % str(e)[:80], "plug in the webcam; Settings > Sound > Input: choose it")

check(bool(shutil.which("aplay") or shutil.which("paplay")), "Speaker player (aplay)", "sudo apt install alsa-utils")
check(bool(shutil.which("espeak-ng") or shutil.which("espeak")), "Offline voice (eSpeak)", "sudo apt install espeak-ng")
piper = os.environ.get("PIPER_MODEL", "")
if piper:
    check(os.path.isfile(piper), "Piper voice " + piper, "fix PIPER_MODEL in visionsync/.env or delete that line")

model = os.environ.get("LOCAL_MODEL", "qwen3:1.7b")
try:
    names = [m.get("name", "") for m in http_json("http://127.0.0.1:11434/api/tags").get("models", [])]
    check(model in names or model + ":latest" in names, "Offline AI model %s in Ollama" % model, "ollama pull " + model)
except Exception:
    check(False, "Ollama (offline AI) is running", "curl -fsSL https://ollama.com/install.sh | sh")

import shutil as _shutil
check(bool(_shutil.which("ffmpeg")), "ffmpeg (your voice is sent 8x smaller: quicker answers)",
      "sudo apt install -y ffmpeg")


def _opus_here():
    import subprocess as _sp
    for codec in (["-c:a", "libopus"], ["-strict", "-2", "-c:a", "opus", "-ar", "48000"]):
        try:
            r = _sp.run([_shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", "16000",
                         "-ac", "1", "-i", "pipe:0"] + codec + ["-f", "ogg", "pipe:1"], input=bytes(16000),
                        stdout=_sp.PIPE, stderr=_sp.DEVNULL, timeout=30)
            if r.returncode == 0 and r.stdout.startswith(b"OggS"):
                return codec[codec.index("-c:a") + 1]
        except Exception:
            pass
    return ""


if _shutil.which("ffmpeg"):
    _codec = _opus_here()
    print("  %s  Opus voice upload%s" % ("OK " if _codec else "-- ", " (" + _codec + ")" if _codec else
                                         ": not possible with this ffmpeg, WAV is sent (works too)"))
try:
    import datetime as _dt
    _now = _dt.datetime.now().astimezone()
    print("  --   Clock: %s %s (UTC%s): reminders and calendar events use this time"
          % (_now.strftime("%Y-%m-%d %H:%M"), _now.tzname(), _now.strftime("%z")))
except Exception:
    pass
try:
    import google_link as _gl
    _g = _gl.account()
    if _g["connected"]:
        check(not _g["problem"], "Google sync with " + (_g["email"] or "the account connected in the app"),
              _g["problem"])
        check(bool(os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()),
              "Google client secret in visionsync/.env (the mirror keeps the app's Google sign-in)",
              "run install.sh again and paste it")
    else:
        print("  --   Google sync: " + (_g["problem"] or _gl.NOT_LINKED))
except Exception as _exc:
    print("  --   Google sync: " + type(_exc).__name__)
try:
    import importlib.util as _ilu
    _piper_in_memory = _ilu.find_spec("piper") is not None
except Exception:
    _piper_in_memory = False
check(_piper_in_memory, "Piper voice kept in memory (quicker first word)",
      "~/.venvs/visionsync-voice/bin/pip install piper-tts")


def http_error_text(e):
    """The short reason inside an HTTP error (OpenAI and Google both answer with JSON)."""
    try:
        err = json.loads(e.read().decode("utf-8") or "{}").get("error") or {}
        return "HTTP %s %s %s" % (e.code, err.get("code") or err.get("status") or "",
                                  str(err.get("message") or "")[:110])
    except Exception:
        return str(e)[:120]


openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
provider = os.environ.get("CLOUD_PROVIDER", "").strip().lower() or ("openai" if openai_key else "gemini")
if provider == "openai":
    oa_model = os.environ.get("OPENAI_MODEL", "gpt-6.1-terra").strip() or "gpt-6.1-terra"
    oa_url = os.environ.get("OPENAI_URL", "https://api.openai.com/v1/responses")
    oa_body = {"model": oa_model, "input": "Say OK.", "max_output_tokens": 16, "store": False}
    effort = os.environ.get("OPENAI_REASONING", "none").strip().lower()
    if effort and effort != "off":
        oa_body["reasoning"] = {"effort": effort}
    oa_auth = {"Authorization": "Bearer " + openai_key}
    if not openai_key:
        check(False, "OPENAI_API_KEY in visionsync/.env (online AI)",
              "bash visionsync-update/install.sh   (it asks for the key)")
    else:
        started = time.time()
        try:
            http_json(oa_url, oa_body, oa_auth, 20)
            check(True, "OpenAI online model %s (answered in %.1f s)" % (oa_model, time.time() - started))
        except urllib.error.HTTPError as e:
            fixes = {401: "the key is wrong or was deleted: make a new one at platform.openai.com/api-keys, then run install.sh again",
                     403: "this key's project may not use this model: check the project's limits at platform.openai.com",
                     404: "this key cannot use %s: set OPENAI_MODEL=gpt-6-luna in visionsync/.env" % oa_model,
                     429: "no credit left (insufficient_quota) or too many requests: check Billing at platform.openai.com"}
            check(False, "OpenAI online model %s (%s)" % (oa_model, http_error_text(e)),
                  fixes.get(e.code, "check OPENAI_MODEL in visionsync/.env"))
        except Exception as e:
            mirror_link.force_ipv4(True)   # no answer at all: try once over IPv4 (some networks break IPv6)
            try:
                http_json(oa_url, oa_body, oa_auth, 20)
                check(False, "OpenAI answers only over IPv4 on this network",
                      "add the line NET_FORCE_IPV4=1 to visionsync/.env, then: systemctl --user restart visionsync-voice")
            except Exception:
                check(False, "OpenAI online model %s (no answer: %s)" % (oa_model, str(e)[:80]),
                      "check the mirror's internet, or connect it to your phone's hotspot")
            finally:
                mirror_link.force_ipv4(os.environ.get("NET_FORCE_IPV4", "").strip() == "1")
    if openai_key and os.environ.get("STT_PROVIDER", "auto").strip().lower() != "local":
        stt_model = os.environ.get("STT_MODEL", "").strip() or "gpt-transcribe"
        try:
            import io
            import wave
            import requests
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\0\0" * 8000)
            started = time.time()
            r = requests.post("https://api.openai.com/v1/audio/transcriptions", headers=oa_auth,
                              files={"file": ("check.wav", buf.getvalue(), "audio/wav")},
                              data={"model": stt_model, "response_format": "json"}, timeout=(5, 20))
            took = time.time() - started
            check(r.status_code == 200, "OpenAI speech-to-text %s (%s)" % (
                stt_model, "answered in %.1f s" % took if r.status_code == 200 else "HTTP %s" % r.status_code),
                "Vision uses Whisper on the mirror instead. To stop trying, add STT_PROVIDER=local to visionsync/.env")
        except Exception as e:
            check(False, "OpenAI speech-to-text (no answer: %s)" % str(e)[:60],
                  "Vision uses Whisper on the mirror instead")
else:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    gemini = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
    if not key:
        check(False, "GEMINI_API_KEY in visionsync/.env (cloud AI)", "free key: https://aistudio.google.com/apikey")
    else:
        try:
            http_json("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % gemini,
                      {"contents": [{"role": "user", "parts": [{"text": "Say OK."}]}]}, {"x-goog-api-key": key}, 20)
            check(True, "Gemini cloud model " + gemini)
        except Exception as e:
            check(False, "Gemini cloud model %s (%s)" % (gemini, str(e)[:100]), "check the key, GEMINI_MODEL and the internet")

check(mirror_link.state() is not None, "Mirror hub (dashboard data)", "sudo systemctl restart visionsync-hub")
for name, url, fix in (
        ("Camera server", os.environ.get("CAMERA_URL", "http://127.0.0.1:8080").rstrip("/") + "/healthz",
         "sudo systemctl restart visionsync-camera"),
        ("Voice assistant service (port 8000)", "http://127.0.0.1:8000/api/health",
         "systemctl --user restart visionsync-voice; journalctl --user -u visionsync-voice -n 40")):
    try:
        http_json(url, timeout=3)
        check(True, name)
    except Exception:
        check(False, name, fix)

print("\nAll good." if not problems else "\n%d thing(s) to fix: see the lines marked FIX." % len(problems))
