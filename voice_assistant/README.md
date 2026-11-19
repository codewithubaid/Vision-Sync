# Vision voice assistant (VisionSync)

Zaema's chatbot, finished and wired into the mirror. On the Jetson it runs as
the user service `visionsync-voice` (set up by `setup_voice.sh`). That service
starts `api_server.py`: the chat API on 127.0.0.1:8000 (the phone chat reaches
it through the camera server), her web page at http://127.0.0.1:8000/dashboard,
spoken reminders, and the hands-free voice loop.

| File | What it does |
| :--- | :--- |
| `voice_service.py` | "Hey Vision", chime, recording, Whisper, answer, voice; mirror states; follow-ups; mute; reminders |
| `ai_router.py` | Her router: tasks, reminders, memories, tool calling. Gemini in the cloud first, Qwen3 on the mirror when offline |
| `mirror_skills.py` | Time, weather, calendar, Wi-Fi, Security / Home Alone / camera, mirror status, with no model |
| `mirror_link.py` | Shared `visionsync/.env`, hub connection, internet check |
| `tts.py` | ElevenLabs, then Piper, then eSpeak, then pyttsx3; the wake chime |
| `doctor.py` | Checks every part and says what to fix |
| `voice_test.py` | Old name, starts `voice_service.py` |

Without a microphone: `python ai_router.py` and type questions.
Tests (no microphone, models or internet needed): `python3 -m unittest test_mirror_integration -v`

On a PC without a microphone, start the API alone:
`VISION_VOICE_AUTOSTART=0 python -m uvicorn api_server:app --port 8000`
