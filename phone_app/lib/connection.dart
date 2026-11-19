import 'dart:async';
import 'dart:io';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'config.dart';
import 'mirror_api.dart';
import 'models.dart';

/// Why the app cannot talk to the mirror. Each one has its own message and fix.
enum ConnIssue { noSecret, noAddress, lanOnly, mirrorOffline, tailscaleOff, authFailed, serverUnavailable, unreachable }

class Diagnosis {
  final ConnIssue issue;
  final String title;
  final String detail;
  const Diagnosis(this.issue, this.title, this.detail);

  /// Problems the user must fix in settings / build, not network trouble.
  bool get isConfig => issue == ConnIssue.noSecret || issue == ConnIssue.authFailed || issue == ConnIssue.noAddress;

  String get message => '$title. $detail';
}

const _kHostPref = 'mirror_host';

/// 100.64.0.0/10 is the address range Tailscale gives to devices.
bool isTailscaleIp(String h) {
  final m = RegExp(r'^100\.(\d{1,3})\.\d{1,3}\.\d{1,3}$').firstMatch(h.trim());
  if (m == null) return false;
  final second = int.parse(m.group(1)!);
  return second >= 64 && second <= 127;
}

bool isTailscaleName(String h) => h.trim().toLowerCase().endsWith('.ts.net');

/// Cleans a Tailscale IP / MagicDNS name typed by the user.
/// Returns '' for empty input and null when it is not a valid host.
String? validateHost(String raw) {
  var s = raw.trim().toLowerCase();
  s = s.replaceFirst(RegExp(r'^https?://'), '');
  if (s.endsWith('.')) s = s.substring(0, s.length - 1);
  if (s.isEmpty) return '';
  if (RegExp(r'^\d{1,3}(\.\d{1,3}){3}$').hasMatch(s)) {
    return s.split('.').every((p) => int.parse(p) <= 255) ? s : null;
  }
  return RegExp(r'^[a-z0-9]([a-z0-9\-.]{0,251}[a-z0-9])?$').hasMatch(s) ? s : null;
}

Future<String> loadHostOverride() async => (await SharedPreferences.getInstance()).getString(_kHostPref) ?? '';

Future<void> saveHostOverride(String host) async {
  final p = await SharedPreferences.getInstance();
  if (host.isEmpty) {
    await p.remove(_kHostPref);
  } else {
    await p.setString(_kHostPref, host);
  }
}

/// Picks the address to use. Order: phone setting, Supabase `tailscale_host`,
/// build-time MIRROR_HOST. The LAN ip is only the last resort (same Wi-Fi only).
Future<Mirror> applyHostOverride(Mirror m) async {
  final o = await loadHostOverride();
  if (o.isNotEmpty) return m.withHost(o);
  if (m.usingTailscale) return m;
  if (kDefaultMirrorHost.isNotEmpty) return m.withHost(kDefaultMirrorHost);
  return m;
}

/// True / false when the phone has / has no Tailscale address; null when this cannot be told.
Future<bool?> phoneHasTailscale() async {
  try {
    final ifs = await NetworkInterface.list(type: InternetAddressType.IPv4);
    if (ifs.isEmpty) return null;
    for (final i in ifs) {
      for (final a in i.addresses) {
        if (isTailscaleIp(a.address)) return true;
      }
    }
    return false;
  } catch (_) {
    return null;
  }
}

/// Turns an error into a clear reason. [secret] is only a parameter so tests can set it.
Future<Diagnosis> diagnoseError(Object e, Mirror m, {String secret = kMirrorSecret, bool? tailscaleOnPhone}) async {
  if (secret.isEmpty) {
    return const Diagnosis(ConnIssue.noSecret, 'Mirror secret not set',
        'Build the app with --dart-define=MIRROR_SECRET=<the same value as MIRROR_SECRET_KEY on the mirror>.');
  }
  if (m.address.isEmpty) {
    return const Diagnosis(ConnIssue.noAddress, 'No mirror address',
        'Set the mirror\'s Tailscale address in Settings.');
  }
  if (e is MirrorAuthException) {
    return const Diagnosis(ConnIssue.authFailed, 'Authentication failed',
        'The mirror rejected the secret. MIRROR_SECRET in the app must match MIRROR_SECRET_KEY on the mirror.');
  }
  final network = e is TimeoutException || e is SocketException || e is http.ClientException;
  if (!network) {
    final what = e is MirrorHttpException ? 'It answered with HTTP ${e.statusCode}' : 'It sent an unexpected answer';
    return Diagnosis(ConnIssue.serverUnavailable, 'Mirror server unavailable',
        '$what. The mirror is reachable, but the camera program (motion_stream_server.py) is not working correctly. Check it is running and up to date.');
  }
  if (!m.usingTailscale && !isTailscaleIp(m.address)) {
    return Diagnosis(ConnIssue.lanOnly, 'No Tailscale address set',
        'The app is using the home-network address ${m.address}, which only works on the mirror\'s Wi-Fi. Set the mirror\'s Tailscale address in Settings.');
  }
  final ts = tailscaleOnPhone ?? await phoneHasTailscale();
  if (ts == false) {
    return const Diagnosis(ConnIssue.tailscaleOff, 'Tailscale disconnected',
        'This phone does not seem to be connected to Tailscale. Open the Tailscale app and turn it on.');
  }
  // Phone-side causes come first. The mirror does not write "last seen" to Supabase yet,
  // so an old value is only a hint that the mirror is off or has no internet.
  if (!m.online) {
    return const Diagnosis(ConnIssue.mirrorOffline, 'Mirror offline',
        'The mirror did not answer and has not reported to the cloud for a while. Check its power and internet.');
  }
  return Diagnosis(ConnIssue.unreachable, 'Mirror not reachable',
      'The mirror is online but did not answer at ${m.address}:${m.port}. Check that Tailscale is running on the mirror, that it is in the same tailnet, and that the camera program is running on port ${m.port}.');
}
