#!/bin/bash
# Opens the dashboard full screen on the mirror's display.
# Uses MagicMirror (port 8181) when it is running, otherwise the hub's own
# page (port 8765), which shows the same widgets.
# Add to "Startup Applications" on the Jetson desktop, or run:  bash kiosk.sh

export DISPLAY="${DISPLAY:-:0}"
MM_URL="http://localhost:8181"
HUB_URL="http://127.0.0.1:8765/"

# Never blank the screen (always-on display, UC-1 step 4).
xset s off 2>/dev/null; xset -dpms 2>/dev/null; xset s noblank 2>/dev/null

# Wait up to 60 s for one of the two pages.
URL=""
for i in $(seq 1 30); do
  if curl -s -o /dev/null --max-time 2 "$MM_URL"; then URL="$MM_URL"; break; fi
  if [ "$i" -ge 10 ] && curl -s -o /dev/null --max-time 2 "${HUB_URL}healthz"; then URL="$HUB_URL"; break; fi
  sleep 2
done
[ -z "$URL" ] && URL="$HUB_URL"
echo "Opening $URL"

BROWSER="$( [ "${KIOSK_BROWSER:-}" = firefox ] || command -v chromium-browser || command -v chromium || command -v google-chrome)"
if [ -z "$BROWSER" ]; then
  # Firefox comes with the Jetson's Ubuntu (KIOSK_BROWSER=firefox in the environment forces it).
  command -v firefox >/dev/null && exec firefox --kiosk "$URL"
  echo "No browser found. Install one: sudo apt install -y chromium-browser"; exit 1
fi

exec "$BROWSER" --kiosk --incognito --noerrdialogs --disable-infobars --disable-session-crashed-bubble \
  --autoplay-policy=no-user-gesture-required --check-for-update-interval=31536000 \
  --overscroll-history-navigation=0 "$URL"
