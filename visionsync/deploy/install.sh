#!/bin/bash
# VisionSync dashboard installer for the Jetson (Orin Nano).
# Run from the project folder:   bash deploy/install.sh
# Safe to run again. It does not touch your .env once it exists.
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(whoami)"
MM_DIR="${MM_DIR:-$HOME/MagicMirror}"
# The camera server lives next to this folder (VisionSync-Final/camera_backend) or in ~/camera_backend.
CAM_DIR=""
for d in "$HERE/../camera_backend" "$HOME/camera_backend"; do
  [ -f "$d/motion_stream_server.py" ] && { CAM_DIR="$(cd "$d" && pwd)"; break; }
done
echo "Project: $HERE   User: $USER_NAME   MagicMirror: $MM_DIR   Camera server: ${CAM_DIR:-not found}"

# 1. Settings file
if [ ! -f "$HERE/.env" ]; then
  cp "$HERE/.env.example" "$HERE/.env"; chmod 600 "$HERE/.env"
  echo ">> Created $HERE/.env  - open it and add your keys."
fi

# 1b. Things the camera server needs. Missing ones are reported, not fatal.
python3 -c "import cv2" 2>/dev/null || echo ">> OpenCV for Python is missing:  sudo apt install -y python3-opencv"
python3 -c "import mediapipe" 2>/dev/null || echo ">> MediaPipe is missing (fall detection will use basic mode):  pip3 install mediapipe"
command -v espeak >/dev/null || echo ">> espeak is missing (the mirror cannot say 'Are you okay?'):  sudo apt install -y espeak"

# 2. Hub self-test (no internet or hardware needed)
python3 -m unittest discover -s "$HERE/hub/tests" 2>&1 | tail -3

# 3. MagicMirror module + config (only if MagicMirror is installed)
if [ -d "$MM_DIR/modules" ]; then
  rm -rf "$MM_DIR/modules/MMM-VisionSync"
  cp -r "$HERE/magicmirror/modules/MMM-VisionSync" "$MM_DIR/modules/"
  [ -f "$MM_DIR/config/config.js" ] && cp "$MM_DIR/config/config.js" "$MM_DIR/config/config.js.bak.$(date +%s)"
  cp "$HERE/magicmirror/config/config.js" "$MM_DIR/config/config.js"
  cp "$HERE/magicmirror/config/custom.css" "$MM_DIR/css/custom.css"
  echo ">> MagicMirror module and config installed (old config.js backed up)."
else
  echo ">> MagicMirror not found at $MM_DIR. The hub's own page (http://127.0.0.1:8765/) will be used."
  echo "   To add MagicMirror later, see README.md section 'MagicMirror2 on the Orin Nano'."
fi

# 3b. Wi-Fi setup mode: phones open the setup page by themselves on the mirror's setup network
if command -v nmcli >/dev/null; then
  sudo mkdir -p /etc/NetworkManager/dnsmasq-shared.d
  sudo cp "$HERE/deploy/visionsync-captive.conf" /etc/NetworkManager/dnsmasq-shared.d/visionsync-captive.conf
  [ -x /usr/sbin/dnsmasq ] || echo ">> dnsmasq is missing (needed for the setup network):  sudo apt install -y dnsmasq-base"
  iw list 2>/dev/null | grep -A 10 "Supported interface modes" | grep -q "\* AP$" \
    || echo ">> Could not confirm this Wi-Fi card can host a setup network. Test with:  sudo python3 $HERE/wifi/visionsync_wifi.py --setup-now"
else
  echo ">> NetworkManager (nmcli) not found: Wi-Fi setup mode will be off."
fi

# 4. Services
for unit in visionsync-hub visionsync-camera visionsync-wifi magicmirror; do
  src="$HERE/deploy/$unit.service"
  [ "$unit" = "magicmirror" ] && [ ! -d "$MM_DIR/modules" ] && continue
  [ "$unit" = "visionsync-camera" ] && [ -z "$CAM_DIR" ] && { echo ">> camera_backend folder not found next to visionsync: skipping its service."; continue; }
  RUN_AS="$USER_NAME"; [ "$unit" = "visionsync-wifi" ] && RUN_AS="root"     # it changes network settings
  sed -e "s#/home/visionsync/camera_backend#$CAM_DIR#g" -e "s#/home/visionsync/visionsync#$HERE#g" -e "s#/home/visionsync#$HOME#g" -e "s#^User=.*#User=$RUN_AS#" "$src" | sudo tee "/etc/systemd/system/$unit.service" >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable "$unit" >/dev/null 2>&1
  sudo systemctl restart "$unit"
  echo ">> $unit: $(systemctl is-active "$unit")"
done

chmod +x "$HERE/deploy/kiosk.sh"
echo
echo "Done. Next:"
echo "  1. nano $HERE/.env          (add keys, then: sudo systemctl restart visionsync-hub)"
echo "  2. bash $HERE/deploy/kiosk.sh   (opens the dashboard full screen)"
echo "  3. Logs: journalctl -u visionsync-hub -f   (Wi-Fi setup: journalctl -u visionsync-wifi -f)"
echo "  4. Try Wi-Fi setup mode now:  sudo python3 $HERE/wifi/visionsync_wifi.py --setup-now"
