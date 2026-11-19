#!/usr/bin/env bash
# VisionSync - let the phone app reach this mirror on any Wi-Fi, hotspot or mobile data
# (Tailscale), and make sure the shared secret is set. At the end it prints what Tania needs.
#   cd ~/Desktop/FYP_Project/visionsync-final && bash visionsync-update/connect_phone.sh
# Safe to run again: it keeps an existing secret and an existing Tailscale sign-in.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ENV_FILE="$ROOT/visionsync/.env"
stop() { echo; echo "STOPPED: $*" >&2; exit 1; }
[ -f "$ENV_FILE" ] || stop "settings file not found: $ENV_FILE (put visionsync-update inside the project folder)"

ts_ip() { (tailscale ip -4 2>/dev/null || sudo tailscale ip -4 2>/dev/null) | head -n1 || true; }

echo "== 1/4 Tailscale on the mirror"
if ! command -v tailscale >/dev/null 2>&1; then
  echo "   Installing Tailscale (needs internet; it may ask for your password)..."
  curl -fsSL https://tailscale.com/install.sh | sh || stop "Tailscale did not install. Check the internet, then run this again"
fi
sudo systemctl enable --now tailscaled >/dev/null 2>&1 || true
TSIP="$(ts_ip)"
if [ -z "$TSIP" ]; then
  echo "   A sign-in link appears below. Open it on your phone or laptop and sign in."
  echo "   Use the SAME account you will use in the Tailscale app on the phone."
  sudo tailscale up --hostname=visionsync-mirror
  TSIP="$(ts_ip)"
fi
[ -n "$TSIP" ] || stop "Tailscale is not signed in yet. Run: sudo tailscale up   then run this script again"
echo "   The mirror's fixed Tailscale address: $TSIP"

echo "== 2/4 Shared secret (the app and the mirror must have the same one)"
SECRET="$(python3 - "$ENV_FILE" <<'PYEOF'
import os, re, secrets, sys, tempfile
path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    lines = f.read().splitlines()
pat = re.compile(r"\s*(?:export\s+)?MIRROR_SECRET_KEY\s*=(.*)$")
values = [m.group(1).strip().strip("'\"") for m in map(pat.match, lines) if m]
value = values[-1] if values else ""          # the camera service uses the last line
placeholders = {"", "change-me", "changeme", "your-secret", "your-own-secret", "your-long-secret",
                "something_long", "YOUR_SECRET"}
if value in placeholders:
    value = secrets.token_urlsafe(24)
    sys.stderr.write("   made a new secret\n")
else:
    sys.stderr.write("   kept the secret that was already set\n")
kept = [l for l in lines if not pat.match(l)] + ["MIRROR_SECRET_KEY=" + value]   # one line only
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)))
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write("\n".join(kept) + "\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
print(value)
PYEOF
)"
[ -n "$SECRET" ] || stop "could not read or write MIRROR_SECRET_KEY in $ENV_FILE"

echo "== 3/4 Restarting the camera server so it uses this secret"
sudo systemctl restart visionsync-camera || stop "could not restart visionsync-camera"
systemctl --user restart visionsync-voice 2>/dev/null || true   # its camera commands use the secret too
for _ in $(seq 1 45); do
  curl -s -m 2 "http://127.0.0.1:8080/healthz" >/dev/null 2>&1 && break
  sleep 1
done

echo "== 4/4 Testing the mirror through Tailscale, the way the phone will"
CODE="$(curl -s -o /dev/null -w '%{http_code}' -m 8 -H "X-Mirror-Secret: $SECRET" "http://$TSIP:8080/status" || true)"
case "$CODE" in
  200) echo "   OK: the mirror answers at $TSIP:8080 and accepts the secret." ;;
  401) stop "the camera server rejected the secret. Check that this shows $ENV_FILE:  systemctl cat visionsync-camera | grep EnvironmentFile" ;;
  *)   stop "no answer from $TSIP:8080 (code $CODE). Check:  sudo systemctl status visionsync-camera" ;;
esac

cat <<EOF

==================================================================
 Send these two values to Tania privately (not in a group chat):
   MIRROR_HOST   = $TSIP
   MIRROR_SECRET = $SECRET

 Tania builds the APK in the phone_app folder with:
   flutter build apk --release --dart-define=MIRROR_SECRET=$SECRET --dart-define=MIRROR_HOST=$TSIP

 On every phone that uses the app: install "Tailscale" from the Play Store,
 sign in with the same account you used here, and switch it on.
 This address stays the same on any Wi-Fi, hotspot or mobile data.
==================================================================
EOF
