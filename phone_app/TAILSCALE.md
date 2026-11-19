# Remote access with Tailscale

The app talks to the mirror through Tailscale (a private, encrypted WireGuard network), so the phone
can be on mobile data or any other Wi-Fi. Nothing is opened to the public internet.

## 1. Set up the tailnet (once)
1. Create a Tailscale account (https://login.tailscale.com) and sign in on **both** devices with the **same account**.
2. **Mirror computer (Linux/Jetson):**
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   tailscale ip -4          # your mirror's Tailscale IP, e.g. 100.101.102.103
   tailscale status         # shows the MagicDNS name, e.g. glass-mirror.tailXXXX.ts.net
   ```
3. **Android phone:** install the Tailscale app from Google Play, sign in, switch it **on**.
4. In the Tailscale admin console, leave key expiry on (more secure) or disable it for the mirror so it
   never needs to sign in again: Machines > mirror > ... > *Disable key expiry*.
5. Do **not** use "Funnel" or port forwarding for port 8080. That would make the mirror public.

## 2. Mirror backend
`motion_stream_server.py` is not part of this zip, so check these in your copy:
- It must listen on `0.0.0.0` and its port (default 8080), for example `HTTPServer(("0.0.0.0", 8080), ...)`.
  Confirm: `ss -tlnp | grep 8080` shows `0.0.0.0:8080`.
- It keeps checking the secret (`X-Mirror-Secret` header, or `?token=` for the video) and answers 401 otherwise.
- Do **not** add `Access-Control-Allow-Origin: *`. A native Android app does not need CORS.

```bash
export MIRROR_SECRET_KEY='a-long-random-secret'     # openssl rand -hex 24
python3 motion_stream_server.py
```
Keep the mirror private: allow port 8080 only through Tailscale and block it on the home network.
```bash
sudo ufw allow in on tailscale0 to any port 8080 proto tcp
sudo ufw deny 8080/tcp
sudo ufw enable
```
(With this, the app works only through Tailscale, even at home. That is the secure choice.)

## 3. Tell the app the address
Priority used by the app: **phone setting** > Supabase `mirrors.tailscale_host` > build-time `MIRROR_HOST` > LAN `ip` (home Wi-Fi only).
- Easiest: app **Settings > Mirror address (Tailscale) > Set**, type the IP or MagicDNS name (no port).
- Or for all users, in Supabase SQL editor:
  ```sql
  alter table mirrors add column if not exists tailscale_host text;
  update mirrors set tailscale_host = '100.101.102.103' where id = '<your mirror id>';
  ```
  Row Level Security on `mirrors` should stay on (each user sees only their mirrors).

## 4. Test the backend before the app (from any device on the tailnet)
```bash
cd mirror_backend
MIRROR_SECRET='a-long-random-secret' python3 check_mirror.py 100.101.102.103 8080
```
It checks /status, /alerts, /events, that a wrong secret is rejected, a camera control call and two video frames.
Run it from a laptop on mobile data (phone hotspot) to prove remote access.

## 5. Build the APK
The secret is never in the source: pass it at build time. It must equal `MIRROR_SECRET_KEY` on the mirror.
```bash
flutter pub get
flutter test --dart-define=MIRROR_SECRET=test-secret
flutter build apk --release \
  --dart-define=MIRROR_SECRET='a-long-random-secret' \
  --dart-define=MIRROR_HOST=100.101.102.103      # optional default address
# output: build/app/outputs/flutter-apk/app-release.apk
```
Anyone who unpacks the APK can read a build-time secret. Share the APK only with people you trust,
and rotate the secret if it leaks. Tailscale access control is the second lock.

## 6. What the app now tells you
| Message | Meaning |
|---|---|
| Mirror secret not set | APK was built without `MIRROR_SECRET` |
| Authentication failed | Secret differs from the mirror's `MIRROR_SECRET_KEY` |
| Mirror offline | The mirror stopped reporting to Supabase (power or internet) |
| No Tailscale address set | Only the LAN address is known; set the Tailscale address |
| Tailscale disconnected | The phone has no Tailscale address: open the Tailscale app |
| Mirror not reachable | Phone has Tailscale, mirror did not answer: Tailscale or camera program down on the mirror |
| Mirror server unavailable | Mirror answered with an error: camera program broken or outdated |
| Stream unavailable | API works but video does not start |

## Limitations
- "Tailscale disconnected" is detected from the phone's network addresses. On some Android versions this
  cannot be read, and the app then reports "Mirror not reachable" instead.
- Plain `http://` stays enabled (Android cannot limit it to Tailscale addresses). Traffic is encrypted by Tailscale.
- Alerts and emergency calls still only run while the app is open.
