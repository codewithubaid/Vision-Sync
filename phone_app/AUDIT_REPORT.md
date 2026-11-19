# Independent Audit Report: Integrated Flutter Final Project (glass_connect)

Audit date: 2026-11-12. Method: static inspection plus the checks that could be run in the audit sandbox.

## 0. Environment limits (read first)
The audit sandbox has **no Flutter/Dart SDK, no Android SDK and no network**. Therefore these were **NOT executed**:
`flutter --version`, `flutter pub get`, `flutter analyze`, `flutter test`, `flutter build apk --debug`, and nothing was run on a device/emulator.
Nothing below that depends on those commands is marked VERIFIED.

What WAS executed successfully:
- `python3 -m py_compile` on every `.py` file (camera_backend, mirror_backend): OK.
- `python3 -m unittest test_security_events` in `camera_backend/`: 12 tests, OK.
- Own script checks on all Dart files: bracket/brace balance (OK after a string/comment-aware pass), every `import 'x.dart'` in `lib/` resolves, direct dependencies in `pubspec.lock` match `pubspec.yaml`.
- Manual line-by-line reading of the camera files, `google_api.dart`, `main.dart`, `config.dart`, Android manifest and Gradle files, and the Settings/Calendar parts of `home_page.dart`.

Not possible: byte-comparison against the two original projects (only the final ZIP was provided). Claims of "byte-identical" in INTEGRATION_REPORT.md are therefore NOT independently confirmed.

## 1. Architecture
Single app, entry `lib/main.dart` (Supabase init, `AuthGate` -> `AuthPage` or `HomeGate`). `HomeGate` -> `HomePage` (tabs: Alerts, Live, Tasks/Calendar/Reminders, Chat, Settings). Mirror server calls via `mirror_api.dart`; Google Tasks/Calendar via `google_api.dart` (token obtained through Supabase Google OAuth); emergency-call logic in `safety_controller.dart`. Partner's camera code is one screen (`direct_camera_page.dart` -> `LiveFeedScreen`) plus `camera_settings.dart` and the local package `third_party/mjpeg_stream`. Single `MaterialApp`; no entry-point conflict.

## 2. Camera audit
Key fact: the partner's "camera" is a **network MJPEG viewer**, not on-device camera access. There is no `camera` plugin, no CAMERA permission, no image capture/saving, no available-camera detection. Those items are NOT APPLICABLE, not missing. The Python server (`camera_backend/motion_stream_server.py`, port 5000, `/video_feed`) runs on a laptop/mirror, not on the phone.

Reviewed: address load/save/validation (`camera_settings.dart`, `SharedPreferencesAsync`), reconnect via keyed subtree, error and loading states, lifecycle in `MJPEGStreamScreen` (request-id guard, client close on dispose, stale-callback guard, stream-ended error, Retry button), Android INTERNET permission and `usesCleartextTraffic=true` (needed for `http://` LAN streams). No genuine integration defect found; no camera code modified.

## 3. Updated features and navigation
All lib files are present and referenced. Settings > "Direct camera stream" pushes `LiveFeedScreen` (home_page.dart:915). The user's own Live tab (`live_page.dart`, `mjpeg_view.dart`) is separate and unchanged. No dummy/placeholder implementations were found in the files read. Whether each button works at runtime is NOT VERIFIED.

## 4. Calendar audit
- Method: **Google Calendar REST API v3 via Google OAuth through Supabase**. Not the device calendar; no Android calendar permission needed.
- Scope requested: `calendar.readonly` + `tasks` (config.dart). **Calendar is read-only.**
- Implemented: retrieval of `primary` calendar events, today + next 2 days, max 50 (`GoogleApi.listEvents`). Date handling avoids the DST +72h bug.
- **NOT implemented: Google Calendar event creation, update, deletion.** (Google *Tasks* add/toggle/delete are implemented.) If the project requirement includes calendar CRUD, it is not met, and adding it also requires changing the scope to a write scope.
- "Reminders" are stored **locally on the phone** (SharedPreferences) and only *displayed* in the app's Calendar view next to Google events. They are not written to Google Calendar. The snackbar text "Reminder saved. It also shows in Calendar." could mislead (it means the in-app Calendar tab). Left unchanged.
- Error handling: 401 / scope errors -> reconnect prompt; other errors -> generic message with retry. Code-reviewed only.
- External setup required (cannot be verified from the ZIP): Supabase project with Google provider enabled; redirect URL `io.supabase.glassconnect://login-callback/` allowed; Google Cloud project with Calendar API and Tasks API enabled and OAuth consent configured (test user added while the app is in testing mode). The Supabase URL and publishable key are hard-coded in `config.dart` (publishable keys are designed to be public, but RLS on `mirrors` must be on, as README notes).
- Mirror features also need `--dart-define=MIRROR_SECRET=...` and **a mirror server that is not in the ZIP** (`/stream.mjpg`, `/status`, `/alerts`, `/chat`...). Live/Safety/Chat tabs cannot work without it. The partner's servers expose different endpoints.

## 5. Technical validation
See section 0. Additional static observations:
- `pubspec.lock` was edited by hand for the path dependency; `flutter pub get` will normalise it. It records `dart >=3.13.0`, `flutter >=3.44.0`, so a **recent Flutter SDK is required** (pubspec itself says `>=3.3.0`).
- `SharedPreferencesAsync` (camera_settings.dart) needs shared_preferences >= 2.3.0: satisfied (lock 2.5.6).
- Android build uses AGP 9.1.0, Kotlin plugin 2.4.0, Gradle 9.3.1 with `android.builtInKotlin=false`/`newDsl=false`. These come from the user's project and were not buildable here: NOT VERIFIED. `android/local.properties` is absent (normal; Flutter creates it).
- `GeneratedPluginRegistrant.java` is committed; Flutter regenerates it.
- `mjpeg_stream` uses `print` and deprecated `withOpacity`; this does not affect `flutter analyze` of the app (path package), only style.
- `direct_camera_page.dart` still says "same Wi-Fi" (partner's text), while the rest of the app moved to Tailscale wording. Cosmetic, unchanged.
- Default direct-camera URL is the partner's old LAN IP `172.20.88.41`; enter your own address and tap Save and connect.
- No `ios/` folder exists (neither original had one), so no iOS configuration exists or can be included.
- No `assets/` folder; none declared in pubspec and none referenced in code: consistent.

## 6. Feature verification table
Status key: VERIFIED = tested successfully; CODE VERIFIED = code inspected, not run; NOT VERIFIED = could not test; FAILED = tested and failed.

### A. Camera functionality
| Feature | Original location | In final? | Result | Issues | Action |
|---|---|---|---|---|---|
| MJPEG viewer widget (reconnect/lifecycle patches) | partner project, patched mjpeg_stream | Yes: third_party/mjpeg_stream | CODE VERIFIED | none found | none |
| Address form, save, validate, reconnect | partner main.dart `LiveFeedScreen` | Yes: lib/direct_camera_page.dart, camera_settings.dart | CODE VERIFIED | default IP is partner's LAN IP; "same Wi-Fi" text | none |
| Camera permission / device camera / capture / save | n/a | Not applicable (network viewer) | NOT VERIFIED (N/A) | no on-device camera exists in either project | none |
| Python MJPEG server + security events | partner camera_backend | Yes | VERIFIED for 12 Python unit tests and compile; server itself not run | needs OpenCV/webcam | none |
| FastAPI backend (camera_backend/backend) | partner | Yes | NOT VERIFIED (deps not installed, not run) | needs requirements, Firebase config | none |
| Camera tests (Dart) | partner test/ | Yes (2 files) | NOT VERIFIED (flutter test not run) | | none |

### B. Updated application features
| Feature | Original location | In final? | Result | Issues | Action |
|---|---|---|---|---|---|
| Auth (Supabase email + Google, password reset) | user's project | Yes | CODE VERIFIED | needs Supabase config | none |
| Home tabs: Alerts, Live, Tasks, Chat, Settings | user's project | Yes | CODE VERIFIED | | none |
| Emergency call / safety logic | user's project | Yes | CODE VERIFIED | works only while app open; needs CALL_PHONE grant | none |
| Tailscale address handling | user's project | Yes | CODE VERIFIED | | none |
| Local tasks and reminders | user's project | Yes | CODE VERIFIED | no notifications | none |
| Mirror server API (Live/Safety/Chat) | user's project | Client only | NOT VERIFIED | server absent from ZIP | none |

### C. Calendar functionality
| Feature | Original | In final? | Result | Issues | Action |
|---|---|---|---|---|---|
| Google OAuth for Calendar | user's project | Yes | CODE VERIFIED | needs Supabase/Google Cloud setup | none |
| Event retrieval (3 days) | user's project | Yes | CODE VERIFIED | not run against real account | none |
| Event creation | n/a | **No** | N/A | not implemented | none (reported) |
| Event update | n/a | **No** | N/A | not implemented | none (reported) |
| Event deletion | n/a | **No** | N/A | not implemented | none (reported) |
| Local reminders shown in Calendar tab | user's project | Yes | CODE VERIFIED | local only, not synced to Google | none |
| Date/DST handling, error handling | user's project | Yes | CODE VERIFIED | | none |
| Device calendar permission | n/a | Not used | N/A | | none |

### D. Navigation and UI
| Feature | Result | Notes |
|---|---|---|
| Single entry point/MaterialApp | CODE VERIFIED | |
| Settings > Direct camera stream route | CODE VERIFIED | home_page.dart:915 |
| All local imports resolve | VERIFIED (script check) | |
| Runtime navigation/buttons | NOT VERIFIED | needs device |

### E. Dependencies and configuration
| Item | Result | Notes |
|---|---|---|
| pubspec.yaml vs pubspec.lock (direct deps) | VERIFIED (script check) | |
| `flutter pub get` resolution | NOT VERIFIED | |
| Path dependency mjpeg_stream | CODE VERIFIED | lock entry hand-edited |
| iOS config | NOT VERIFIED | no ios/ in either project |

### F. Android build and execution
| Item | Result | Notes |
|---|---|---|
| `flutter analyze` | NOT VERIFIED | no SDK |
| `flutter test` | NOT VERIFIED | no SDK |
| `flutter build apk --debug` | NOT VERIFIED | no SDK; AGP 9.1.0 / Kotlin 2.4.0 / Gradle 9.3.1 untested |
| Manifest permissions (INTERNET, CALL_PHONE, cleartext, deep link) | CODE VERIFIED | |
| Run on device | NOT VERIFIED | |

## 7. Modifications made
**No source code was modified.** Only changes to the delivered ZIP: added this `AUDIT_REPORT.md`; set the executable bit on `android/gradlew` (it was stored non-executable; harmless, helps on Linux/macOS).

## 8. Unresolved issues / risks
1. Flutter analyze/test/build never executed by anyone in this chain (previous integration and this audit): the project is **not proven to compile**. Run the commands below before submitting.
2. Calendar create/update/delete not implemented; Calendar is read-only.
3. Mirror server for Live/Safety/Chat is missing from the ZIP.
4. Google/Supabase external setup cannot be confirmed.
5. Android toolchain versions (AGP 9.1.0, Kotlin 2.4.0, Gradle 9.3.1) unverified.
6. Security notes carried over: Google token in plain SharedPreferences, cleartext HTTP allowed, release builds signed with debug key.

## 9. How to run
```bash
cd Integrated_Flutter_Final_Project
flutter --version            # needs Flutter >= 3.44 (Dart >= 3.13), per pubspec.lock
flutter pub get
flutter analyze
flutter test
flutter build apk --debug --dart-define=MIRROR_SECRET=your-secret
flutter run --dart-define=MIRROR_SECRET=your-secret --dart-define=MIRROR_HOST=<mirror tailscale ip>
```
Direct camera: on the laptop run `python camera_backend/motion_stream_server.py` (needs `opencv-python`), then in the app: Settings > Direct camera stream > enter `http://<laptop-ip>:5000/video_feed` > Save and connect (phone and laptop on the same network).
If any command fails, send me its full output and I will diagnose it.
