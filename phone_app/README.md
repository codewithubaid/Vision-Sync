> **Merged project:** this app now also contains the VisionSync direct camera screen (Settings > Direct camera stream)
> and the partner's Python camera side in `camera_backend/`. See `INTEGRATION_REPORT.md` first.

# Glass Connect

Flutter app (Android) to control a smart mirror / camera: alerts, live video, fall
detection with emergency calls, Google Tasks and Calendar.

## Run

```bash
flutter pub get
flutter run --dart-define=MIRROR_SECRET=your-own-secret --dart-define=MIRROR_HOST=<mirror tailscale ip>
```

`MIRROR_SECRET` must be exactly the same as `MIRROR_SECRET_KEY` on the mirror
(`motion_stream_server.py`). There is no built-in default: without it the app says the secret is not set.
Remote access (mobile data, other Wi-Fi) works through Tailscale: see **TAILSCALE.md**.

## Supabase

- Add `io.supabase.glassconnect://login-callback/` to *Authentication > URL
  Configuration > Redirect URLs*.
- Turn on Row Level Security for the `mirrors` table so each user only sees their own
  mirrors. The app reads the table with no user filter.

## Good to know

- Alerts are only checked while the app is open. Automatic emergency calls do not work
  with the phone locked or the app closed.
- Alerts older than 10 minutes (by the mirror's clock) never start automatic calls.
  Keep the mirror's clock synced.
- Phone and mirror must both be signed in to the same Tailscale network (TAILSCALE.md). They no longer need the same Wi-Fi.
- Before a release build, create a real signing key (`android/app/build.gradle.kts`
  still signs with the debug key).

## Tests

```bash
flutter test
```
