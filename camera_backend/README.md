# VisionSync camera server (merged)

One program on the Jetson owns the webcam and serves the phone app and the
mirror dashboard on port 8080.

```bash
MIRROR_SECRET_KEY=your-long-secret python3 motion_stream_server.py
```

`deploy/install.sh` in the `visionsync` folder sets it up as a service that
starts on boot and reads `MIRROR_SECRET_KEY` from `visionsync/.env`.

## What was merged

| From | What |
| :--- | :--- |
| First camera backend (port 8080) | Everything the app's Alerts, Live, Safety and Chat tabs use; kept as the base |
| Partner's FastAPI backend | Pose-based fall detection (`safety.py`), now `pose_fall.py` and wired into Home Alone mode |
| Partner's port-5000 stream | The `/video_feed` address, now on port 8080 and protected by the secret key |

## Addresses

All need `X-Mirror-Secret: <key>` or `?token=<key>`, except `/healthz`.

| Address | Use |
| :--- | :--- |
| `GET /stream.mjpg`, `GET /video_feed` | Live video (same picture) |
| `GET /status` | Modes, camera state, temperature, Wi-Fi, `fall_detector` |
| `GET /alerts`, `GET /events?limit=50` | Open alerts, history |
| `POST /mode/security`, `/mode/home_alone` | `{"enabled": true}` |
| `POST /camera/toggle` | `{"enabled": false}` also releases the webcam, so its light goes off |
| `POST /alerts/dismiss` | `{"id": "..."}` or `{}` for all |
| `POST /settings/resolution`, `/settings/voice_mute`, `/settings/ai_mode` | App and hub settings |
| `POST /chat`, `/calls/log` | App chat, emergency call log |
| `POST /devices` | `{"token": "..."}` registers a phone for push notifications |
| `POST /notifications/test` | Sends a test push to registered phones |
| `GET /healthz` | "Server is up", no key needed |

## Fall detection

- Put `pose_landmarker_full.task` in `models/` for best accuracy. The lite
  model is already there and is used if nothing better is found.
- Needs `pip3 install mediapipe`. If that is missing, the server uses the older
  shape-based check and says so in `/status` (`"fall_detector": "shape"`).
- A fall is: upright, then lying within 3 seconds, seen twice in a row. Then
  "Are you okay?" and a 30-second countdown. Standing up cancels it. No
  recovery raises FALL DETECTED for the phone to call the contacts.
- Lying down that did not follow standing (sofa, bed) does not start a countdown.
- "Are you okay?" for a motionless person is asked only while a person is in
  view. An empty room no longer triggers it.

## Push notifications (optional, free)

`push.py` sends Security alert, FALL DETECTED, no-response and camera-offline
alerts to registered phones through Firebase Cloud Messaging, so they arrive
when the app is closed. If the Jetson is offline, alerts wait (up to 24 hours)
and go out when the internet returns. Off until `FIREBASE_SERVICE_ACCOUNT` is
set. Setup: `phone_app/PUSH-SETUP.md`.

## Settings (environment, or lines in `visionsync/.env`)

| Name | Default | Meaning |
| :--- | :--- | :--- |
| `MIRROR_SECRET_KEY` | (must set) | Same value as in the phone app |
| `SERVER_PORT` | 8080 | |
| `CAMERA_SOURCE` | auto | `auto`, a number such as `0`, or a video file for testing |
| `FIREBASE_SERVICE_ACCOUNT` | (off) | Path to the Firebase key file, turns push on |
| `FALL_DETECTOR` | auto | `shape` forces the old check |
| `POSE_MODEL_PATH` | (models folder) | Full path to a `.task` file |
| `POSE_FPS` | 4 | Pose checks per second |
| `FALL_COUNTDOWN_SECONDS` | 30 | SRS value |
| `MOTIONLESS_SECONDS` | 60 | Stillness before "Are you okay?" |
| `MOTIONLESS_GRACE_SECONDS` | 15 | SRS value |

## Tests

```bash
python3 -m unittest test_security_events test_push     # 41 tests, no camera or internet needed
```
