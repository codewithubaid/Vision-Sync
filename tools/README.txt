tools/ - setup helpers for the mirror (run on the Jetson, from the project folder)

  bash tools/voice_setup.sh
      Asks for your OpenAI API key (hidden), writes the voice settings to visionsync/.env,
      runs the offline tests, restarts the voice assistant and runs doctor.py.

  bash tools/connect_phone.sh
      Installs Tailscale, sets the shared secret, tests the camera server through Tailscale
      and prints MIRROR_HOST, MIRROR_SECRET and the exact APK build command.

Voice settings in visionsync/.env (after a change: systemctl --user restart visionsync-voice):
  OPENAI_MODEL=gpt-6-luna        smarter: gpt-6.1-terra
  STT_PROVIDER=auto              local = speech never leaves the mirror
  BARGE_IN=on                    words = only "Hey Vision" / "stop" / "wait" interrupt; off = never
  WAKE_WORDS=hey vision          add ",vision" to also wake on "Vision" alone
  END_SILENCE_SECONDS=0.7        longer if it cuts you off, shorter for quicker replies
  WAKE_NEAR=1.5, VOICE_FOCUS=0.25    lower them if Vision ignores you, raise them in a loud hall
