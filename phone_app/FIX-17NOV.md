# Phone app update (18 Nov, final): 8 files

Copy these over your project, keeping the same paths:

- `lib/home_page.dart`
- `lib/mirror_api.dart`
- `lib/connection.dart`
- `lib/models.dart`
- `lib/google_api.dart`
- `lib/config.dart`
- `lib/main.dart`
- `test/widget_test.dart`

## What changed

- **The mirror syncs the Google account connected in the app.** No account is set on the mirror:
  right after "Connect Google", the app gives the mirror that account's Google sign-in. The mirror
  then keeps its tasks the same as Google Tasks and its reminders the same as Google Calendar.
  Connecting another account switches the mirror to that account; "Disconnect" unlinks it.
- **One status line under the Tasks / Calendar / Reminders tabs** says which account the mirror
  syncs, or what is missing, with a "Sync mirror" button.
- **No more Google page every hour:** the app gets a fresh Google token from the mirror.
- **Google scopes:** `calendar.events` (instead of `calendar.readonly`) + `tasks`, so the mirror can
  put reminders said to Vision in the calendar. Everyone taps "Sync mirror" (or Connect Google) once.
- Tasks and reminders said to Vision show in the app within 10 seconds. Each item shows once, even
  when it is on the mirror and on Google. A due date picked for a task is kept.
- Ticking a reminder done stops Vision saying it; swiping it away deletes it everywhere.
- Ticking, adding or deleting a Google task in the app tells the mirror at once.
- If the mirror can't be reached, the app saves tasks and reminders on the phone as before.
- The "Mirror is offline" banner shows only when the phone really cannot reach the mirror, and
  "Tailscale disconnected" comes first if the phone's Tailscale is off.

## Build

Ubaid sends you two values privately: `MIRROR_HOST` (an address like 100.x.x.x) and `MIRROR_SECRET`.

```
flutter test     # all tests should pass
flutter build apk --release --dart-define=MIRROR_SECRET=<secret> --dart-define=MIRROR_HOST=<100.x.x.x>
```

The APK is at `build/app/outputs/flutter-apk/app-release.apk`.

## On each phone

Install Tailscale from the Play Store, sign in to Ubaid's Tailscale network and switch it on. Open
the app, then Tasks > "Connect Google" (or "Sync mirror"), and allow Tasks and Calendar.
