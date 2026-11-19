# VisionSync integration contract

> Done: the voice assistant (`../voice_assistant/`) uses `visionsync_client.py` exactly as
> section 1 describes, and also sends tasks and reminders to the mirror with
> `vs.agenda(tasks, reminders)` (state section `agenda`, shown bottom right).

How each teammate's part plugs into the dashboard. Nothing here needs extra
Python packages.

```
 USB mic/speaker            USB camera                     Phone (Flutter app)
       |                        |                                 |
 voice pipeline          motion_stream_server.py  <---- HTTP :8080 (secret key)
 (wake word, STT,         (stream, Security Mode,
  LLM, TTS)                Home Alone, alerts)
       |                        ^
       | Unix socket            | HTTP :8080 on 127.0.0.1 (secret key)
       v                        v
      +--------------------------------+        OpenWeatherMap
      |   VisionSync hub (Python)      | <----  Google Calendar (.ics)
      |   hub/visionsync_hub.py        |
      +--------------------------------+
        | Unix socket /tmp/visionsync.sock        | HTTP :8765 (127.0.0.1 only)
        v                                         v
  MagicMirror2 :8181                       hub's own dashboard page
  (MMM-VisionSync module)                  (same widgets, no Node needed)
```

## Ports on the Jetson

| Port | Who | Reachable from |
| :--- | :--- | :--- |
| 8080 | Camera backend (`motion_stream_server.py`) | Phone app (LAN / Tailscale), secret key |
| 8181 | MagicMirror2 | Mirror only |
| 8765 | VisionSync hub | Mirror only |

MagicMirror's default port is also 8080. It is moved to 8181 in
`magicmirror/config/config.js` so it does not clash with the camera backend.

## 1. Voice pipeline (M4, M5, M6)

Copy `hub/visionsync_client.py` next to your script.

```python
from visionsync_client import VisionSync
vs = VisionSync(role="voice")

# in your main loop
if vs.muted():                 # user muted the assistant in the app
    continue
# ... wake word detected, chime played
vs.listening()
# ... recording finished, Whisper running
vs.transcribing()
# ... text ready
vs.thinking(heard=text)
use_cloud = vs.online() is not False       # hub knows within ~1.5 s (NFR-P4)
prompt_context = vs.context()              # live weather + next events as text
# ... LLM answered, TTS about to play
vs.speaking(reply, mode="cloud" if use_cloud else "local")
# ... audio finished
vs.idle()
```

Other calls: `vs.not_understood()` (low STT confidence, UC-2 alt 2),
`vs.failed()` (cloud and local model both failed, UC-2 alt 3),
`vs.set_mode("local")`, `vs.throttled()` (Jetson is hot: skip heavy work).

Every call returns `False`/`None` instead of raising when the hub is not
running, so the voice loop never depends on the dashboard.

What the mirror shows for each state:

| State | Mirror |
| :--- | :--- |
| `idle` | thin line, "Say “Hey Vision”" |
| `listening` | blue moving wave, "Listening" |
| `transcribing`, `thinking` | what was heard, slow wave |
| `speaking` | the reply in large type, white wave, note when answered offline |
| `error` | the message in amber |

Spoken text leaves the screen 20 s after the last update and is never written
to disk (NFR-SEC3).

Not using Python? Send the same thing over HTTP from the Jetson:

```bash
curl -X POST http://127.0.0.1:8765/api/voice \
     -d '{"state":"speaking","reply":"Hello","mode":"cloud"}'
```

or write one JSON object per line to the Unix socket:
`{"type":"voice","state":"listening"}`.

### Wi-Fi by voice (works with no internet)

```python
r = vs.wifi_setup()                      # "set up Wi-Fi": QR code on the mirror
r = vs.wifi_connect("Home", "password")  # "connect to Home, the password is ..."
r = vs.wifi_off(); r = vs.wifi_on()
r = vs.wifi_status()                     # {"online": True, "connected_to": "Home", ...}
speak(r["said"])                         # every call returns a sentence to speak back
```

Match these with keywords or the **local** model. When Wi-Fi is the problem
the cloud model cannot be reached. Prefer `wifi_setup()`: a password spoken
aloud is heard by everyone in the room and is easily misheard.

## 2. Camera / security backend (M7, UC-3)

No code changes needed. The hub reads these endpoints once a second with the
`X-Mirror-Secret` header:

| Endpoint | Used for |
| :--- | :--- |
| `GET /status` | Security / Home Alone mode, camera on or paused, stream size, voice mute |
| `GET /alerts` | Person detected, fall detected, no response, camera offline |
| `GET /events?limit=25` | The "Are you okay?" countdown (kept in history, not in active alerts) |
| `POST /settings/ai_mode` | Hub tells the backend cloud / local, so the app's Status tab is right |
| `POST /settings/resolution` | Hub drops the stream to 480p while the board is over 85 C, restores it after |

Requirement: backend and hub must have the same `MIRROR_SECRET_KEY`.
`deploy/visionsync-camera.service` starts the backend from the same `.env`.

## 3. Phone app (M8)

No changes needed. Mode switches, mute and alert dismissals made in the app
show on the mirror within about a second, because the hub reads them from the
camera backend.

## 4. Reading the hub (any part)

```bash
curl http://127.0.0.1:8765/api/state     # everything, as JSON
curl http://127.0.0.1:8765/healthz
```

Sections of the state: `weather`, `calendar`, `system` (cpu_temp, cpu_percent,
ram_percent, wifi_dbm, online, throttled), `ai` (mode), `voice`, `camera`,
`alerts`, `hub`.

## 5. Demo without hardware

```bash
python3 hub/visionsync_hub.py --demo          # then open http://127.0.0.1:8765/
curl -X POST http://127.0.0.1:8765/api/demo/fall     # jump to one scene
```

Scenes: clear, listening, thinking, speaking, not_heard, offline,
offline_speaking, security, intruder, home_alone, fall, fall_recovered,
emergency, camera_offline, hot, muted, setup.
