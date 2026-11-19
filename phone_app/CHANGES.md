# Tailscale remote access

- `models.dart`: `Mirror.tailscaleHost`, `address`, `usingTailscale`, `withHost` (reads `tailscale_host` from Supabase).
- `config.dart`: stream URL uses the Tailscale address; the built-in demo secret is gone (`MIRROR_SECRET` is required);
  optional `MIRROR_HOST` build default.
- `connection.dart` (new): address priority, host validation, and the diagnosis (offline / Tailscale off / auth / server / unreachable).
- `mirror_api.dart`: all requests use `mirror.address`; 8 s timeout; one retry of reads on a dropped connection; `MirrorHttpException`.
- `safety_controller.dart`, `safety_page.dart`, `home_page.dart`, `chat_page.dart`, `live_page.dart`, `home_gate.dart`:
  every "same Wi-Fi" message replaced by the specific diagnosis; "Stream unavailable" notice; Settings card to set the address.
- `mirror_backend/check_mirror.py` (new): end-to-end checker. `TAILSCALE.md` (new): setup guide.

# Final review pass

## Build (Android)
- `android/app/build.gradle.kts`: added the `org.jetbrains.kotlin.android` plugin. `gradle.properties`
  sets `android.builtInKotlin=false`, so without it `MainActivity.kt` is not compiled and the
  `kotlin { }` block does not resolve.
- `AndroidManifest.xml`: added `flutter_deeplinking_enabled=false`. Without it the Supabase login
  link (`io.supabase.glassconnect://login-callback/?code=...`) is also treated as a Flutter route and
  fails with "Could not find a generator for route" after Google login / email links.
- `AndroidManifest.xml`: added the `https` VIEW query so the browser can be opened for Google login
  on Android 11+.

## App
- `safety_controller.dart`: with an alert waiting and no contact set, the call sequence was
  restarted every 3 seconds. It now waits until a contact is added.
- `home_gate.dart`: the mirror list no longer fails when the `mirrors` table has no `created_at`
  column; the "Connect to your mirror" screen now has a Sign out button (before, a user with no
  mirrors was stuck there).
- `home_page.dart`: sign-out stops polling first, so cleared data is not written back.
- `safety_page.dart`: the wrong-secret banner no longer tells you to edit `kMirrorSecret` in the
  source (the secret now comes from `--dart-define=MIRROR_SECRET`); no `BuildContext` use after an
  `await` in the settings switches.

# What was fixed

## Build / tests
- `test/widget_test.dart`: replaced the template counter test (wrong package name, missing
  `MyApp`) with real tests of the app logic.
- `pubspec.yaml`: added the missing `flutter_test` dev dependency.

## Emergency calls (`safety_controller.dart`, `safety_page.dart`)
- Handled alert ids are saved on the phone, and alerts older than 10 minutes never start
  automatic calls, so opening the app or switching mirrors no longer re-dials for an old alert.
- A finished call sequence (answered / failed / no contact) no longer blocks a new alert.
- A retry that comes due while the app is in the background (phone call open) no longer
  dials over the call. When you return, the app asks "Did they answer?" with
  "They answered" / "Call again".
- Adding a contact after "no contact" lets the waiting alert start its calls.
- Phone numbers are cleaned and checked before saving.
- Command errors now tell "can't reach the camera" apart from "unexpected answer".
- Home Alone text now says it only works while the app is open.

## UI / state
- `home_page.dart`: refresh the mirror right away (no false "Mirror offline" for 30 s);
  "due today" card now says "due or overdue"; task delete/toggle no longer fail silently when
  the Google token is missing; Google loading can't run twice at once; background re-login
  errors are handled; sign-out clears contacts, call log and handled alerts.
- `live_page.dart`: full screen no longer opens a second stream, and stops in the background.
- `mjpeg_view.dart`: a stream that connects but never sends frames is now retried; the LIVE
  badge no longer shows an old state.
- `main.dart`: the reset-password dialog stays open until the password is valid.
- `auth_page.dart`: Google sign-in failures are shown; no setState after dispose.
- `chat_page.dart`: error messages are no longer sent back as chat history.

- A failed call (for example no phone permission) now says so instead of "Called ...".
- An alert with no timestamp is treated as new, so it can still start calls.

## Data
- Day labels and the 3-day calendar window no longer break on daylight-saving change days.
- `models.dart`: mirror id is now a String (works with UUIDs); `last_seen` without a time zone
  is read as UTC.
- `mirror_api.dart`: an empty 200 reply counts as success.
- `google_api.dart`: only real auth problems (401, scope errors) disconnect Google; an old or
  rejected token is not given a fresh 55 minutes.

## Security / config
- `config.dart`: secret can be set with `--dart-define=MIRROR_SECRET=...`.

## NOT changed (needs your decision)
- Turn on Row Level Security for `mirrors` in Supabase (the app lists all rows it can see).
- Google token is still saved in plain SharedPreferences (use `flutter_secure_storage`).
- Cleartext HTTP to the mirror is still allowed (Android cannot limit it to a LAN range).
- Alerts and calls only work while the app is open (needs a foreground service).
- Release builds still use the debug signing key.
- No `ios/` folder.

# Review fixes (clock + address)
- `mirror_api.dart`: measures phone-vs-mirror clock difference from the HTTP `Date` header (`MirrorApi.clockSkew`).
- `safety_controller.dart`: `isTooOld` uses that difference, so a wrong mirror clock no longer silently blocks calls for real fall alerts.
- `home_page.dart`: clearing the mirror address in Settings now reverts immediately, even offline.
- `test/widget_test.dart`: new test for the clock-skew case.

# Manual tasks and reminders (+ button)
- `local_store.dart` (new): tasks and reminders typed in by hand, saved on the phone (SharedPreferences). Cleared on sign-out.
- `home_page.dart`: the + button is always on the Tasks screen (before, only when Google was connected).
  Tasks tab: + adds a task (to Google Tasks when connected, otherwise on the phone). Reminders tab (new): + adds a reminder
  with a date and time; tick to finish, swipe left to delete. Calendar tab: + adds a reminder and reminders for the next
  3 days are shown with the Google events. Alerts tab: new "reminder today or overdue" card.
- `home_gate.dart`: sign-out also clears the hand-typed tasks and reminders.
- `test/widget_test.dart`: tests for reminder and task JSON round trips.
- Not included: reminders do not ring or send notifications (needs flutter_local_notifications).
