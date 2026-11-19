# VisionSync camera app

This Android Flutter app displays the MJPEG feed from the Python camera server.
The current learning step adds an editable stream address that is saved on the
phone and restored when the app opens again. Motion detection and alerts are
planned for a later step.

## Run this step

1. Connect the laptop and phone to the same Wi-Fi network.
2. Start the existing `stream_server.py` on the desktop:
   `python ../stream_server.py` from this project's terminal.
3. Run `ipconfig` on the laptop and find the IPv4 address of its Wi-Fi adapter.
4. Connect the Android phone by USB, with USB debugging enabled.
5. Run `flutter pub get`, then `flutter run`.
6. In the app, enter `http://YOUR_LAPTOP_IP:5000/video_feed` and tap
   **Save and connect**.

After adding a native Flutter plugin, stop and rerun the app; hot reload alone
does not install the plugin's Android code.

## What changed

- `lib/main.dart`: the screen keeps track of its current address. A form lets
  the user edit it, save it, and restart the camera viewer. Reconnect retries
  the active address without saving unfinished edits.
- `lib/camera_settings.dart`: validates address format and uses
  `SharedPreferencesAsync` to store one address on the phone.
- `android/app/src/main/AndroidManifest.xml`: declares Internet permission for
  release builds as well as debug builds.
- `test/camera_app_test.dart`: checks address validation, restoration, saving,
  error handling, and reconnect behavior with a pretend camera.
- `third_party/mjpeg_stream`: the original MIT-licensed camera viewer, with a
  small lifecycle patch to close old requests when reconnecting. Its separate
  regression tests cover requests finishing after the old viewer closes.

The first launch still uses the previous address as a starting value. Replace
it with your laptop's current address. A successful save means the setting was
stored; it does not prove the camera is online. The camera preview reports
stream loading/errors separately.

## Check on the phone

1. Save the correct address and confirm that live video appears.
2. Close and reopen the app; confirm the address is remembered.
3. Enter an invalid address, such as `hello`, and tap **Save and connect**.
   Confirm an explanation appears and the current stream address is unchanged.
4. Stop/restart the server and use the reconnect icon in the app bar.

Use only a trusted local network for this prototype: the existing server uses
HTTP and does not authenticate viewers.

Run automated checks with `flutter analyze` and `flutter test`. These checks
cannot replace trying the real camera and saved settings on an Android phone.
