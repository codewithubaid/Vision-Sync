# VisionSync: final package

Everything for the mirror is in this folder. Copy `VisionSync-Final` to the
Jetson, for example to `~/Desktop/FYP_Project/VisionSync-Final`.

| Folder | What it is |
| :--- | :--- |
| `visionsync/` | Mirror dashboard and the hub that joins every part; Wi-Fi setup with a QR code |
| `camera_backend/` | Camera, Security Mode, Home Alone fall detection, phone API (port 8080) |
| `voice_assistant/` | Zaema's chatbot, finished: "Hey Vision", Whisper, Gemini online, Qwen3 offline, voice, tasks, reminders |
| `phone_app/` | Flutter app (build it on a PC) |
| `install_all.sh` | Installs all of it on the Jetson |

How the parts talk: phone app → camera server (8080) → voice assistant (8000) for chat;
voice assistant, camera server and Wi-Fi service → hub (8765) → mirror screen.

## 1. Install (once, needs internet, about 20 minutes)

```bash
cd ~/Desktop/FYP_Project/VisionSync-Final
bash install_all.sh
```

It asks for your password. It installs the dashboard, camera server and Wi-Fi
setup, then the voice assistant (Python packages in their own folder, Whisper,
Ollama with qwen3:1.7b), takes the Vosk and Piper models from
`~/Desktop/FYP_Project/Models`, opens the dashboard full screen at every login
and turns on automatic login.

## 2. Keys

```bash
nano ~/Desktop/FYP_Project/VisionSync-Final/visionsync/.env
```

| Line | Where to get it |
| :--- | :--- |
| `MIRROR_SECRET_KEY` | Make up a long one; put the same one in the phone app |
| `OPENWEATHER_API_KEY` | openweathermap.org, free |
| `GOOGLE_CALENDAR_ICS_URL` | Google Calendar > Settings > your calendar > "Secret address in iCal format" |
| `OPENAI_API_KEY` | platform.openai.com/api-keys (the online brain and speech-to-text). Easiest: run `bash tools/voice_setup.sh`, which asks for it and writes the voice settings |
| `GEMINI_API_KEY` | optional alternative online brain: aistudio.google.com/apikey (`CLOUD_PROVIDER=gemini`) |

The voice is Piper or eSpeak, both on the Jetson; no paid voice service is used.

Save with Ctrl+O, Enter, Ctrl+X. In Ubuntu Settings > Sound choose your microphone
as Input and the USB speaker as Output (the voice assistant follows these choices). Then `sudo reboot`.
To reach the mirror from the phone on any network, run `bash tools/connect_phone.sh`.

## 3. Check

```bash
~/.venvs/visionsync-voice/bin/python ~/Desktop/FYP_Project/VisionSync-Final/voice_assistant/doctor.py
```

Every line should say OK. A FIX line says what to do.

## 4. Test plan

Stand in front of the mirror and say:

| # | Say or do | You should see and hear | SRS |
| :--- | :--- | :--- | :--- |
| 1 | "Hey Vision" | chime, "Listening" with the wave | UC-2 step 1 |
| 2 | "What is the weather today?" | the weather, spoken; question and answer on the mirror | UC-2 steps 2–4 |
| 3 | "Who wrote Hamlet?", then without the wake word "When was he born?" | the second answer uses the first | UC-2 step 5 |
| 4 | Cough or mumble | "I did not catch that. Please repeat your question." | UC-2 alternate 2 |
| 5 | Say nothing for 8 seconds | back to "Say Hey Vision" | UC-2 alternate 1 |
| 6 | "Remind me in two minutes to drink water" | saved; two minutes later a chime, spoken and shown | reminders |
| 7 | "Add buy milk to my tasks" | appears under To do, bottom right | tasks |
| 8 | "What time is it?", "What's on my calendar today?" | answered from the dashboard data | UC-1, UC-2 |
| 9 | "Turn on security mode", then "Turn off security mode" | turns on; turning off is refused (phone app only) | UC-3 |
| 10 | "Is home alone mode on?" | answer from the camera server | UC-3 |
| 11 | Unplug the internet, wait 2 s, "Tell me a joke" | status shows the mirror's own AI; Qwen3 answers | UC-4, NFR-P4 |
| 12 | Internet back, ask again | cloud again | UC-4 step 4 |
| 13 | "Turn off the Wi-Fi", "Turn on the Wi-Fi", "Set up Wi-Fi" | done; setup shows the QR code | Wi-Fi by voice |
| 14 | "Connect to (network name), the password is (password)" | joins that network | Wi-Fi by voice |
| 15 | Phone app chat | the same assistant answers | integration |
| 16 | Mute in the phone app | mirror shows muted and ignores "Hey Vision" | SRS 4.1 Settings |
| 17 | "Goodbye" | back to standby | |

Timing for NFR-P1 (3 s) is written to `~/.cache/visionsync/voice_timings.csv`
(numbers only, no words). Spoken passwords lose their spaces; for a password
with spaces or capitals use "Set up Wi-Fi" and the QR code.

## 5. If something is wrong

```bash
systemctl --user status visionsync-voice        # voice assistant
journalctl --user -u visionsync-voice -f        # its log (VISION_DEBUG=1 in .env shows the words)
systemctl status visionsync-hub visionsync-camera visionsync-wifi
espeak-ng "hello"                               # speaker test
arecord -d 3 /tmp/t.wav && aplay /tmp/t.wav     # microphone test
```

| Problem | Fix |
| :--- | :--- |
| No reaction to "Hey Vision" | Settings > Sound > Input: the webcam; speak a bit closer |
| Answers slow | keep `WHISPER_MODEL=base.en`; run doctor.py to check the Gemini key |
| "AI assistant is temporarily limited." | Ollama could not load the model: run `ollama run qwen3:1.7b` to see why |
| No dashboard on screen | `bash visionsync/deploy/kiosk.sh` (Chromium, else Firefox) |

## 6. What changed in the chatbot (for Zaema)

- Runs on the Jetson as a service; the Windows-only parts (beep, "Zira" voice) are replaced.
- Shows every step on the mirror (listening, thinking, speaking, cloud or local) and obeys mute from the app.
- Hybrid routing: skips the cloud at once when the hub reports no internet (within 2 s), pauses
  the cloud when the free quota runs out, and goes back to the cloud when the internet returns.
- Uses the exact SRS sentences for "did not catch", "unable to process" and "temporarily limited".
- New mirror skills: time, weather, calendar, Wi-Fi (on, off, set up, connect, status), Security and
  Home Alone Mode, camera, mute, mirror status. Turning protection off by voice is refused.
- Due reminders are now spoken and shown (before, they were marked done silently).
  Tasks and reminders appear on the dashboard. The phone app's chat uses this assistant.
- Fixed: Gemini tool calls (thought signature, call id, deprecated temperature) and a possible
  double action when falling back; Gemini text read as UTF-8 (degree sign and quotes); "good bye"
  not ending the chat; "I finished my homework" and "What do I need to do to make tea?" taken as
  task commands; "remind you to about the meeting"; "a valid your mother's name"; vague
  "delete the first one" no longer sent to tool calling; Python 3.8 support (JetPack 5).
- Privacy (NFR-SEC3): no WAV file, no words in the logs, the conversation is cleared after each session.
- Faster: 0.8 s end of speech, no spoken greeting after the wake word, beam size 1, model warm-up,
  the local model stays loaded.
- Left out: the router backups, gemini_test.py (old model name), the Windows install notes.

## 7. Not tested here

These need the Jetson: the real microphone and speaker, Vosk, Whisper and Ollama on the board,
OpenAI with a real key (the code follows its current documentation), Piper's
command line (if it differs, eSpeak is used), the Wi-Fi hotspot on your Wi-Fi card, automatic
login, and the Flutter build.

## 8. Differences from the SRS to write in the report

- Cloud model: Gemini 3.8 Flash instead of GPT-4, because it has a free tier (OR-1).
- Local model: Qwen3 1.7B in Ollama on the GPU instead of LLaMA 3 or Gemma.
- Whisper runs on the CPU (faster-whisper, int8): there is no GPU build of it for the Jetson through pip.
- Google Calendar through its secret iCal address instead of OAuth.
- Hardware: Jetson Orin Nano 8 GB with its own power supply and DisplayPort, not the Jetson Nano 2/4 GB.
- NFR-P1 is measured from the end of the question to the start of the spoken answer.
- iOS was not built or tested (NFR-Q4).
