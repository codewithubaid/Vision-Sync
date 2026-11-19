#!/bin/bash
# VisionSync: installs every part on the Jetson. Run once (needs internet):
#   bash install_all.sh
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "== Dashboard, hub, camera server, Wi-Fi setup"
bash "$HERE/visionsync/deploy/install.sh" || echo "!! The dashboard installer reported a problem (see above)."

echo "== Voice assistant"
bash "$HERE/voice_assistant/setup_voice.sh"

echo "== Mirror screen starts by itself"
mkdir -p "$HOME/.config/autostart"
cat > "$HOME/.config/autostart/visionsync-kiosk.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=VisionSync mirror
Exec=bash "$HERE/visionsync/deploy/kiosk.sh"
X-GNOME-Autostart-enabled=true
EOF
# Never blank or lock the screen behind the mirror.
gsettings set org.gnome.desktop.session idle-delay 0 2>/dev/null
gsettings set org.gnome.desktop.screensaver lock-enabled false 2>/dev/null
gsettings set org.gnome.desktop.screensaver idle-activation-enabled false 2>/dev/null
# Log in by itself after power on: there is no keyboard behind the mirror.
CONF=/etc/gdm3/custom.conf
if [ -f "$CONF" ]; then
  sudo sed -i -E -e "s/^#?\s*AutomaticLoginEnable\s*=.*/AutomaticLoginEnable=true/" \
                 -e "s/^#?\s*AutomaticLogin\s*=.*/AutomaticLogin=$USER/" "$CONF"
  grep -q "^AutomaticLogin=" "$CONF" || sudo sed -i "/^\[daemon\]/a AutomaticLoginEnable=true\nAutomaticLogin=$USER" "$CONF"
  echo "Automatic login: on for $USER"
fi

echo
echo "Done. Add your keys:   nano $HERE/visionsync/.env"
echo "Then restart:          sudo reboot"
