# VisionSync: a smart mirror on a Jetson Orin Nano

Final year project, BS Computer Science, Forman Christian College (A Chartered University).

VisionSync is a mirror that shows your day, talks with you and keeps your home safe. It runs on an
NVIDIA Jetson Orin Nano (8 GB) and works with a companion Android app.

## What it does

- **Dashboard**: time, weather, calendar, tasks, reminders and alerts on the mirror.
- **Voice assistant "Hey Vision"**:
  - Speech-to-text: OpenAI when online, or Whisper on the Jetson when offline.
  - Answers: OpenAI `gpt-6-luna` when online, or Qwen3 on the Jetson when offline.
  - Voice: Piper or eSpeak, both on the Jetson.
  - It speaks while the answer is still being written. It goes quiet as soon as you talk over it, and only listens to the person standing at the mirror.
- **Security Mode**: detects movement and alerts your phone.
- **Home Alone Mode**: detects falls and alerts your emergency contacts.
- **Phone app**: live camera, modes, alerts and chat, from any network through Tailscale. Tasks and reminders are shared with the mirror.

## Folders

| Folder | What is in it |
|---|---|
| `visionsync/` | Hub, dashboard, Wi-Fi setup, system services, settings template (`.env.example`) |
| `voice_assistant/` | The Hey Vision assistant: wake word, speech-to-text, AI router, voice |
| `camera_backend/` | Camera server, motion and fall detection, push alerts |
| `phone_app/` | Flutter Android app |
| `tools/` | `voice_setup.sh` (OpenAI key and voice settings) and `connect_phone.sh` (Tailscale and secret) |
| `archive/` | Earlier partner code, kept for reference and not used on the mirror |

## Set up

See **START-HERE.md** for the full guide. In short, on the Jetson:

```bash
bash install_all.sh              # services, voice assistant, kiosk dashboard
bash tools/voice_setup.sh        # asks for your OpenAI key, checks everything
bash tools/connect_phone.sh      # Tailscale + shared secret; prints the APK build command
```

Keys live only in `visionsync/.env` on the mirror. That file is listed in `.gitignore`, so it is never uploaded.

## Google sync

The mirror syncs the Google account connected in the phone app; no account is set on the mirror.
Right after "Connect Google" (Supabase Google sign-in with offline access), the app hands the
mirror that account's Google sign-in through the camera server (protected by the mirror secret).
The mirror then keeps its tasks the same as Google Tasks and its reminders the same as Google
Calendar, in both directions: add, tick or delete by voice, in the app or in Google, and the others
follow (mirror to Google in about 2 seconds, Google to the mirror within 30 seconds). Timed events
in Google Calendar for the next 7 days become reminders that Vision says on time. Connecting another
account switches the mirror to it; "Disconnect" in the app unlinks it.

To keep the sign-in, the mirror needs VisionSync's own Google OAuth client secret: the Web client
that Supabase uses for Google sign-in. `install.sh` asks for it and keeps it in `visionsync/.env`
as `GOOGLE_CLIENT_SECRET`. It identifies the app to Google, not a person. The sign-in itself is kept
in `~/.config/visionsync/google_token.json` on the Jetson and is git-ignored.

## Team

- Ubaid: dashboard and integration
- Tania: Android app
- Alina: camera and motion detection
- Zaema: hybrid chatbot

Supervisor: M. Rauf Butt · Co-advisor: Sharoon Nasim
