#!/usr/bin/env bash
# VisionSync voice setup (final, 18 Nov):
#   - OpenAI becomes the online AI (fast model, no thinking); Qwen3 stays the offline backup
#   - quicker answers: online speech-to-text, one shared connection, voice spoken sentence by sentence
#   - talk over Vision and it stops at once (barge-in)
#   - the microphone is the one chosen in Settings > Sound > Input
#   - Google: the mirror syncs the tasks and calendar of the account connected in the phone app
#   cd ~/Desktop/FYP_Project/visionsync-final && bash visionsync-update/install.sh
# Safe to run again. Your current files are backed up first and put back if a check fails.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VA="$ROOT/voice_assistant"
ENV_FILE="$ROOT/visionsync/.env"
PY="$HOME/.venvs/visionsync-voice/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

stop() { echo; echo "STOPPED: $*" >&2; exit 1; }
[ -f "$VA/ai_router.py" ] || stop "put the visionsync-update folder inside the project folder, next to voice_assistant (looked for $VA/ai_router.py)"
[ -f "$ENV_FILE" ] || stop "settings file not found: $ENV_FILE"

echo "== 1/5 Backing up the files this update replaces"
BK="$ROOT/backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BK"
for f in ai_router.py api_server.py doctor.py mirror_link.py voice_service.py tts.py test_mirror_integration.py \
         google_link.py google_sync.py; do
  if [ -f "$VA/$f" ]; then cp -p "$VA/$f" "$BK/"; fi
done
mkdir -p "$BK/camera_backend"
cp -p "$ROOT/camera_backend/motion_stream_server.py" "$BK/camera_backend/" 2>/dev/null || true
cp -p "$ROOT/camera_backend/agenda.py" "$BK/camera_backend/" 2>/dev/null || true
cp -p "$ENV_FILE" "$BK/env.backup"
chmod 600 "$BK/env.backup"
echo "   saved in $BK"
put_back() {
  cp -p "$BK"/*.py "$VA/"
  if [ -f "$BK/camera_backend/motion_stream_server.py" ]; then cp -p "$BK/camera_backend/motion_stream_server.py" "$ROOT/camera_backend/"; fi
  if [ -f "$BK/camera_backend/agenda.py" ]; then cp -p "$BK/camera_backend/agenda.py" "$ROOT/camera_backend/"; fi
  stop "$1 - your previous files were put back, nothing else changed"; }

echo "== 2/5 Installing the new files"
if [ -d "$HERE/files" ]; then
  cp "$HERE"/files/voice_assistant/*.py "$VA/"
  cp "$HERE/files/visionsync/.env.example" "$ROOT/visionsync/.env.example"
  cp "$HERE"/files/camera_backend/*.py "$ROOT/camera_backend/"
else
  echo "   (the files are already in place)"
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "   installing ffmpeg (your voice is sent 8x smaller, so answers come quicker; it may ask your password)"
  sudo apt-get install -y ffmpeg >/dev/null 2>&1 \
    || { sudo apt-get update -qq >/dev/null 2>&1 && sudo apt-get install -y ffmpeg >/dev/null 2>&1; } \
    || echo "   (ffmpeg could not be installed: Vision sends normal WAV audio instead)"
fi
"$PY" -m py_compile "$VA/ai_router.py" "$VA/api_server.py" "$VA/doctor.py" "$VA/mirror_link.py" "$VA/voice_service.py" \
  "$VA/tts.py" "$VA/google_link.py" "$VA/google_sync.py" "$ROOT/camera_backend/agenda.py" "$ROOT/camera_backend/motion_stream_server.py" \
  || put_back "a file did not compile"

echo "== 3/5 Offline tests (no internet, no cost, about 15 seconds)"
(cd "$VA" && "$PY" -m unittest -q test_openai_router test_voice_fast test_google_sync) || put_back "the tests failed"
(cd "$ROOT/camera_backend" && "$PY" -m unittest -q test_agenda) || put_back "the tests failed"

echo "== 4/5 Your keys (kept only in visionsync/.env, never shown)"
HAVE="$(grep -E '^[[:space:]]*(export[[:space:]]+)?OPENAI_API_KEY=[^[:space:]]' "$ENV_FILE" || true)"
if [ -n "$HAVE" ]; then
  echo "   A key is already saved (not shown). Press Enter to keep it, or paste a new one."
fi
KEY=""
read -rsp "   Paste the key (it starts with sk-; nothing shows while you paste), then press Enter: " KEY || true
echo
KEY="$(printf '%s' "$KEY" | tr -d '[:space:]')"
if [ -z "$KEY" ] && [ -z "$HAVE" ]; then
  stop "no key was pasted. The new files are installed; run this script again to add the key"
fi
case "$KEY" in
  ""|sk-*) ;;
  *) stop "that is not an OpenAI key (it must start with sk-). Your settings were not changed" ;;
esac

echo
echo "   Google: the mirror syncs the account connected in the phone app. To keep that sign-in it needs"
echo "   VisionSync's Google client secret (the same Web client Supabase uses for Google sign-in):"
echo "   console.cloud.google.com > APIs & Services > Credentials > OAuth 2.0 Client IDs > that Web client."
if grep -qE '^[[:space:]]*(export[[:space:]]+)?GOOGLE_CLIENT_SECRET=[^[:space:]]' "$ENV_FILE"; then
  echo "   A Google client secret is already saved (not shown). Press Enter to keep it."
fi
GSECRET=""
read -rsp "   Paste the Google client secret (often starts with GOCSPX-), or press Enter: " GSECRET || true
echo
GSECRET="$(printf '%s' "$GSECRET" | tr -d '[:space:]')"

NEW_KEY="$KEY" NEW_GSECRET="$GSECRET" "$PY" - "$ENV_FILE" <<'PYEOF'
import os, re, sys, tempfile
path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    lines = f.read().splitlines()

def current(name):
    for line in lines:
        m = re.match(r"\s*(?:export\s+)?%s\s*=(.*)$" % name, line)
        if m and m.group(1).strip().strip("'\""):
            return m.group(1).strip().strip("'\"")
    return ""

want = {
    "CLOUD_PROVIDER": "openai",
    "OPENAI_API_KEY": os.environ.get("NEW_KEY", "").strip() or current("OPENAI_API_KEY"),
    "OPENAI_MODEL": current("OPENAI_MODEL") or "gpt-6-luna",          # fast and cheap
    "OPENAI_REASONING": current("OPENAI_REASONING") or "none",        # answer at once, no thinking
    "STT_PROVIDER": current("STT_PROVIDER") or "auto",                # OpenAI speech-to-text when online
    "MIC_DEVICE": "pipewire",                                         # the mic chosen in Settings > Sound
    "END_SILENCE_SECONDS": current("END_SILENCE_SECONDS") or "0.7",
    "WHISPER_THREADS": current("WHISPER_THREADS") or "6",
    "BARGE_IN": current("BARGE_IN") or "on",
    "GOOGLE_CLIENT_SECRET": os.environ.get("NEW_GSECRET", "").strip() or current("GOOGLE_CLIENT_SECRET"),
}
if not want["GOOGLE_CLIENT_SECRET"]:
    del want["GOOGLE_CLIENT_SECRET"]          # not given yet: Google sync waits for it
if not want["OPENAI_API_KEY"]:
    sys.exit("STOPPED: no OpenAI key found")
NOTES = {"# Online AI: OpenAI (written by visionsync-update). Qwen3 stays the offline backup.",
         "# Voice settings (written by visionsync-update)."}
names = "|".join(want)
kept = [l for l in lines
        if l.strip() not in NOTES and not re.match(r"\s*(?:export\s+)?(?:%s)\s*=" % names, l)]
while kept and not kept[-1].strip():
    kept.pop()
kept += ["", "# Voice settings (written by visionsync-update)."] + ["%s=%s" % item for item in want.items()]
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)))
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write("\n".join(kept) + "\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
print("   saved: OpenAI %s, online speech-to-text, barge-in on, mic = Settings > Sound > Input (key hidden)"
      % want["OPENAI_MODEL"])
print("   Google client secret: %s" % ("saved (hidden)" if "GOOGLE_CLIENT_SECRET" in want
                                      else "not given yet - run this script again to add it"))
PYEOF

echo "== 5/5 Restarting the voice assistant, then checking everything"
if systemctl --user restart visionsync-voice 2>/dev/null; then
  echo "   voice assistant restarted"
else
  echo "   (could not restart visionsync-voice: start the voice assistant the way you normally do)"
fi
echo "   restarting the camera server (the phone app reads the mirror's reminders through it)"
sudo systemctl restart visionsync-camera 2>/dev/null || echo "   (could not restart visionsync-camera: run ~/vs-restart.sh)"
sleep 3
(cd "$VA" && "$PY" doctor.py) || true
echo
(cd "$VA" && "$PY" google_link.py status) | sed 's/^/   /' || true
echo
echo "Finished. In Settings > Sound > Input, choose your USB mono mic (volume about 80%)."
echo "Then say \"Hey Vision\" and ask something. While it answers, just start talking: it stops and listens."
echo "Google: in the phone app open Tasks and tap Connect Google (or Sync mirror). The mirror then keeps"
echo "that account's tasks and calendar: say \"Hey Vision, add buy milk to my tasks\" and it shows in Google."
echo "Your previous files are in $BK"
