// Push notifications through Firebase Cloud Messaging.
//
// Copy this file over lib/push.dart AFTER running `flutterfire configure`
// (see PUSH-SETUP.md). It needs lib/firebase_options.dart, which that command creates.
//
// What it does: gets this phone's Firebase token and gives it to the mirror
// (POST /devices). From then on the mirror can send "FALL DETECTED",
// "Security alert" and "Camera offline" to this phone even when the app is closed.
import 'dart:async';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';

import 'firebase_options.dart';
import 'mirror_api.dart';

// Runs when a message arrives while the app is closed. Android shows the
// notification by itself, so there is nothing to do here.
@pragma('vm:entry-point')
Future<void> _onBackgroundMessage(RemoteMessage message) async {}

class Push {
  static bool _ready = false;
  static String? _sentToken;
  static String? _sentTo;

  static bool get enabled => _ready;

  /// Call once from main(), before runApp. Never throws: without Firebase the
  /// app simply runs without push.
  static Future<void> init() async {
    try {
      await Firebase.initializeApp(options: DefaultFirebaseOptions.currentPlatform);
      FirebaseMessaging.onBackgroundMessage(_onBackgroundMessage);
      // Firebase sometimes issues a new token: forget the old one so the next
      // mirror refresh (every 30 s) registers the new one.
      FirebaseMessaging.instance.onTokenRefresh.listen((_) => _sentToken = null);
      _ready = true;
    } catch (_) {
      _ready = false;
    }
  }

  /// Gives this phone's token to the mirror. Safe to call often: it only talks
  /// to the mirror when the token or the mirror address changed.
  static Future<void> registerWith(MirrorApi api) async {
    if (!_ready) return;
    try {
      final messaging = FirebaseMessaging.instance;
      final settings = await messaging.requestPermission(); // Android 13+ asks the user once
      if (settings.authorizationStatus == AuthorizationStatus.denied) return;
      final token = await messaging.getToken();
      if (token == null || token.isEmpty) return;
      final where = '${api.mirror.address}:${api.mirror.port}';
      if (token == _sentToken && where == _sentTo) return;
      await api.registerDevice(token);
      _sentToken = token;
      _sentTo = where;
    } catch (_) {
      // Mirror unreachable or no internet: the next refresh tries again.
    }
  }
}
