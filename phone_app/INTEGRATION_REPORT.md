# Integration report

Base project: **glass_connect** (your app). Merged in: the partner's **VisionSync camera_app**.
One Flutter app, one entry point (`lib/main.dart`), package name `glass_connect`, Android applicationId unchanged.

## STATUS OF VALIDATION (read first)
The integration was done in a sandbox with **no Flutter/Dart SDK and no network**. These were therefore **NOT run**:
`flutter pub get`, `flutter analyze`, `flutter test`, `flutter build apk --debug`, and nothing was run on a device.
What WAS done: static checks (all imports resolve, braces balanced, no public-name clashes, every package used is declared),
byte-comparison against both originals, `py_compile` of all Python, and the partner's 12 Python security-event tests (pass).
Run the four Flutter commands yourself before submitting (see "Manual steps").

## 1. Integrated from the partner's project
- `third_party/mjpeg_stream/` - patched MJPEG viewer (reconnect/lifecycle fixes), byte-identical, added as a path dependency.
- `lib/camera_settings.dart` - saved stream address + URL validation, byte-identical (line endings only).
- `lib/direct_camera_page.dart` - the partner's `LiveFeedScreen` (address form, Save and connect, Reconnect, live preview), taken from their `main.dart`.
- `test/camera_stream_lifecycle_test.dart` (identical) and `test/direct_camera_test.dart` (ported: package name, no `MyApp`).
- `camera_backend/` - all of the partner's Python side: `motion_stream_server.py` (port 5000, `/video_feed`), `security_events.py`,
  FastAPI `backend/` (+ pose model, `.env.example`), their tests and helper scripts, README and note.

## 2. Retained from your project (unchanged)
Everything else: auth/Supabase, Google Calendar + Tasks, mirror pages (Live, Safety/emergency calls, Chat, Settings), Tailscale support,
theme, `mirror_backend/check_mirror.py`, all Android files, `test/widget_test.dart`, docs.

## 3. How the camera code was preserved
Partner camera code is copied, not rewritten. Only their own `main()` and `MaterialApp` wrapper (`MyApp`) were removed to avoid a second entry point.
Note: the partner's "camera" is a network MJPEG viewer, not on-device camera access, so there is no Android CAMERA permission to carry over.
Its INTERNET permission is already in your manifest, and your `usesCleartextTraffic="true"` is what lets it open `http://` LAN streams.
It is reachable from **Settings > Direct camera stream**. Your own Live tab (`mjpeg_view.dart`) is untouched and still the main viewer.

## 4. Calendar
No partner file touches Calendar, so `google_api.dart`, `main.dart`, `auth_page.dart`, the deep-link filter and
`flutter_deeplinking_enabled=false` are byte-identical to yours. Code review of what exists: Google Calendar REST, **read-only**
(`calendar.readonly`), today + next 2 days. Event create/update/delete is NOT implemented (only Tasks add/toggle/delete).
It uses Google login through Supabase, not the phone's calendar, so no Android calendar permission is needed.

## 5. Files modified / added
- `lib/home_page.dart` (+11 lines): import and one Settings card opening `LiveFeedScreen`.
- `pubspec.yaml`: added `mjpeg_stream` path dependency. `pubspec.lock`: added the matching path entry (copied from the partner's lock format).
- Added: `lib/camera_settings.dart`, `lib/direct_camera_page.dart`, `third_party/`, `camera_backend/`, two tests, this report.

## 6. Conflicts / dependency issues
- Both locks were resolved on Dart >=3.13 / Flutter >=3.44 with identical `http 1.6.0` and `shared_preferences 2.5.6`: no version conflict.
- `http` was dev-only in the partner's pubspec; it is a normal dependency in yours (kept).
- Android: partner's gradle/manifest are a subset of yours, so yours was kept (includes your Kotlin-plugin fix).
- Two entry points / two `MaterialApp`s resolved to one. No class-name clashes (`LiveFeedScreen` vs your `LivePage`).
- Not carried over: partner's `windows/` runner, `.iml` files, `devtools_options.yaml`, `.github/modernize` (Copilot hooks), `lib/motion_test.py` moved to `camera_backend/`.
  Your app is Android-only; their Windows runner would need regenerating for the new plugins.

## 7. Tests actually completed
Static checks and Python tests only (see top). Flutter tests exist (yours + 2 ported partner files) but were not executed.

## 8. Manual steps / not verifiable here
1. `flutter pub get && flutter analyze && flutter test && flutter build apk --debug`
2. **Server gap:** the server your Live/Safety/Chat tabs call (`/stream.mjpg`, `X-Mirror-Secret`, `/status`, `/alerts`, `/mode/*`, `/chat`, `/calls/log`)
   is **not in either archive**. The partner's servers expose different APIs (`/video_feed` on 5000; FastAPI `/cameras/...` on 8000).
   Those tabs need your mirror server. The Direct camera screen works with the partner's `motion_stream_server.py`.
3. Build with `--dart-define=MIRROR_SECRET=...` (required, no default) and optionally `MIRROR_HOST=...`.
4. Direct camera default URL is the partner's old LAN IP `172.20.88.41`; enter yours and Save.
5. Supabase: Google provider enabled, redirect `io.supabase.glassconnect://login-callback/` allowed; Calendar and Tasks APIs enabled in Google Cloud.
6. Needs a real phone/camera/Google account to verify: live video, reconnect, Google login, calendar list, emergency calls.
7. No `ios/` folder existed in either project, so no iOS configuration was merged.
