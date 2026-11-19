# Switching on push notifications (Firebase, free)

Until you do this, the app builds and works as before, with alerts shown only
while the app is open. After it, the mirror can alert a locked phone.

You need: a Google account, a PC with Flutter, and Node.js (for the Firebase tool).

## A. Firebase project (5 minutes, in the browser)

1. Go to https://console.firebase.google.com and sign in.
2. "Create a project". Name it, for example, `visionsync`. Google Analytics can be off.
   The free Spark plan is the default; no card is asked for.

## B. Connect the app (on the PC, in the `phone_app` folder)

```bash
npm install -g firebase-tools
firebase login
dart pub global activate flutterfire_cli
flutter pub add firebase_core firebase_messaging
flutterfire configure
```

In `flutterfire configure` pick your project and tick **android**. It creates
`lib/firebase_options.dart` and wires the Android build files for you.

Then switch the real push code on:

```bash
copy push_setup\push_firebase.dart lib\push.dart        (Windows)
cp push_setup/push_firebase.dart lib/push.dart          (Mac / Linux)
flutter run --dart-define=MIRROR_SECRET=your-secret --dart-define=MIRROR_HOST=jetson-ip
```

Allow notifications when the phone asks.

## C. Give the mirror permission to send (browser, then the Jetson)

1. Firebase console > gear icon > Project settings > **Service accounts** >
   "Generate new private key". A `.json` file downloads. Keep it private: it is
   a password. Never put it in the app or on GitHub.
2. Copy it to the Jetson, for example as `/home/YOU/visionsync-firebase.json`, then:

```bash
chmod 600 ~/visionsync-firebase.json
echo "FIREBASE_SERVICE_ACCOUNT=$HOME/visionsync-firebase.json" >> ~/VisionSync-Final/visionsync/.env
sudo systemctl restart visionsync-camera
```

The Jetson needs internet to send (and `python3-cryptography`, normally present).

## D. Test

Open the app once while it can reach the mirror, then on the Jetson:

```bash
S=your-secret
curl -s -H "X-Mirror-Secret: $S" http://127.0.0.1:8080/status | tr ',' '\n' | grep push
curl -s -X POST -H "X-Mirror-Secret: $S" http://127.0.0.1:8080/notifications/test
```

`push_ready` should be `true` and `push_devices` at least 1. Lock the phone
before the second command: a "VisionSync test" notification should arrive.

| `/status` shows | Meaning |
| :--- | :--- |
| `"push_error": "FIREBASE_SERVICE_ACCOUNT is not set"` | Step C not done, or the service was not restarted |
| `"push_error": "Firebase key file not found..."` | Wrong path in `.env` |
| `"push_devices": 0` | The app has not registered: step B not finished, notifications denied, or the app cannot reach the mirror |
| `"push_queued"` stays above 0 | The Jetson has no internet; it sends when it is back |

## What gets pushed

Person detected in Security Mode, FALL DETECTED, no response after "Are you
okay?", and camera offline. The 30-second countdown itself is not pushed.

## Limits

- Tapping the notification opens the app; it does not jump to the alert yet.
- iOS is not set up (the project has no iOS folder).
- This was written without a Flutter build or a real Firebase project. The
  mirror side was tested against a pretend Firebase server.
