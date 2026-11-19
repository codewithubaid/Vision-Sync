// Push notifications: OFF in this file.
//
// This placeholder keeps the app building before Firebase is set up. To switch
// push notifications on, follow PUSH-SETUP.md: it replaces this file with
// push_setup/push_firebase.dart.
import 'mirror_api.dart';

class Push {
  /// True once Firebase is set up (never, in this placeholder).
  static bool get enabled => false;

  static Future<void> init() async {}

  static Future<void> registerWith(MirrorApi api) async {}
}
