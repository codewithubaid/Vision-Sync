#!/bin/bash
# Sets up the voice assistant on the Jetson (once; safe to run again):
#   bash voice_assistant/setup_voice.sh
# Needs internet once: Python packages, the Whisper model, the local AI model.
HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="${VENV:-$HOME/.venvs/visionsync-voice}"
MODELS="${MODELS_DIR:-$HOME/Desktop/FYP_Project/Models}"
ENV_FILE="$HERE/../visionsync/.env"
say() { echo; echo "== $*"; }

say "1/6 System packages"
sudo apt-get update -qq
sudo apt-get install -y python3-venv python3-pip libportaudio2 espeak-ng alsa-utils unzip curl

say "2/6 Python packages (own folder: $VENV)"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip wheel
"$VENV/bin/pip" install -q -r "$HERE/requirements.txt" || { echo "!! Python packages failed. Check the internet and run this again."; exit 1; }
"$VENV/bin/pip" install -q piper-tts || echo ">> Piper (natural offline voice) did not install. eSpeak will be used offline."

say "3/6 Wake-word model (Vosk)"
if [ ! -f "$HERE/vosk-model/am/final.mdl" ]; then
  rm -rf "$HERE/vosk-model"
  D="$(find "$MODELS" -maxdepth 4 -type f -path '*vosk-model*small*/am/final.mdl' 2>/dev/null | head -1)"
  [ -z "$D" ] && D="$(find "$MODELS" -maxdepth 4 -type f -path '*vosk-model*/am/final.mdl' 2>/dev/null | head -1)"
  if [ -n "$D" ]; then
    ln -s "$(dirname "$(dirname "$D")")" "$HERE/vosk-model"
  else
    Z="$(find "$MODELS" -maxdepth 3 -type f -iname 'vosk-model*small*.zip' 2>/dev/null | head -1)"
    [ -z "$Z" ] && Z="$(find "$MODELS" -maxdepth 3 -type f -iname 'vosk-model*.zip' 2>/dev/null | head -1)"
    if [ -z "$Z" ]; then Z=/tmp/vosk-small.zip; curl -L -o "$Z" https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip; fi
    rm -rf /tmp/vosk-unzip && mkdir -p /tmp/vosk-unzip && unzip -q "$Z" -d /tmp/vosk-unzip
    F="$(find /tmp/vosk-unzip -path '*/am/final.mdl' | head -1)"
    [ -n "$F" ] && mv "$(dirname "$(dirname "$F")")" "$HERE/vosk-model"
  fi
fi
[ -f "$HERE/vosk-model/am/final.mdl" ] && echo "Wake-word model ready." || echo "!! Wake-word model missing."

say "4/6 Speech-to-text model (Whisper, about 150 MB, downloaded once)"
( cd "$HERE" && "$VENV/bin/python" -c "
import os, mirror_link
from faster_whisper import WhisperModel
WhisperModel(os.environ.get('WHISPER_MODEL') or 'base.en', device='cpu', compute_type='int8', download_root='models/whisper')
print('Whisper ready.')" ) || echo "!! Whisper download failed. Run this script again with internet."

say "5/6 Offline AI model (Ollama + qwen3:1.7b)"
command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh
LOCAL_MODEL="$(grep -E '^LOCAL_MODEL=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2-)"
ollama pull "${LOCAL_MODEL:-qwen3:1.7b}" || echo "!! Could not download the local model. Later run: ollama pull qwen3:1.7b"
# A natural offline voice: use a Piper voice from the Models folder if there is one.
if [ -f "$ENV_FILE" ] && ! grep -qE '^PIPER_MODEL=.+' "$ENV_FILE"; then
  P="$(find "$MODELS" -maxdepth 4 -type f -name '*.onnx' -path '*iper*' 2>/dev/null | grep -i 'en_' | head -1)"
  [ -z "$P" ] && P="$(find "$MODELS" -maxdepth 4 -type f -name '*.onnx' -path '*iper*' 2>/dev/null | head -1)"
  if [ -n "$P" ] && [ -f "$P.json" ]; then echo "PIPER_MODEL=$P" >> "$ENV_FILE"; echo "Piper voice: $P"; fi
fi

say "6/6 Start now and at every login"
mkdir -p "$HOME/.config/systemd/user"
sed -e "s#@VENV@#$VENV#g" -e "s#@HERE@#$HERE#g" "$HERE/visionsync-voice.service" > "$HOME/.config/systemd/user/visionsync-voice.service"
systemctl --user daemon-reload
systemctl --user enable visionsync-voice >/dev/null 2>&1
systemctl --user restart visionsync-voice
sleep 5
echo "visionsync-voice: $(systemctl --user is-active visionsync-voice)"
"$VENV/bin/python" "$HERE/doctor.py"
