import 'dart:async';
import 'dart:convert';
import 'dart:io' show Platform;
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_phone_direct_caller/flutter_phone_direct_caller.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:url_launcher/url_launcher.dart';
import 'config.dart';
import 'connection.dart';
import 'mirror_api.dart';
import 'models.dart';

/// Alerts older than this are still shown, but they never start automatic calls
/// (so opening the app hours after an old, undismissed alert does not call anyone).
/// This compares the mirror's timestamp with the phone's clock, so keep the
/// mirror's clock synced (NTP).
const kAutoCallMaxAge = Duration(minutes: 10);

const _kRetryGap = Duration(seconds: 60);

/// If the app is in the background when a retry is due (usually because the phone
/// call screen is open), wait this long for the user to come back before dialing again.
const _kMaxBackgroundWait = Duration(minutes: 3);

/// One emergency call sequence (SRS UC-3): primary x3, 1 minute apart, then secondary.
class Escalation {
  final SafetyAlert alert;
  int primaryTries = 0;
  bool secondaryDone = false;
  // calling | waiting | confirm | answered | failed | nocontact
  String state = 'calling';
  String text = 'Starting emergency calls…';
  DateTime? nextAt;
  DateTime? backgroundSince;
  Escalation(this.alert);

  /// The sequence is over (it no longer blocks a new alert from starting its own).
  bool get finished => state == 'answered' || state == 'failed' || state == 'nocontact';
}

class CallRecord {
  final int time;
  final String who;
  final String phone;
  final int attempt;
  final String result;
  CallRecord(this.time, this.who, this.phone, this.attempt, this.result);

  Map<String, dynamic> toJson() => {'t': time, 'who': who, 'phone': phone, 'n': attempt, 'r': result};
  factory CallRecord.fromJson(Map<String, dynamic> j) =>
      CallRecord((j['t'] as num).toInt(), j['who'] as String, j['phone'] as String, (j['n'] as num).toInt(), j['r'] as String);
}

/// Keeps the app and the camera in sync: polls status and alerts every 3 seconds,
/// sends commands, and runs the emergency call sequence.
class SafetyController extends ChangeNotifier {
  SafetyController(this.mirror);

  Mirror mirror;
  MirrorApi get _api => MirrorApi(mirror);

  MirrorStatus? status;
  List<SafetyAlert> alerts = [];
  List<SafetyAlert> events = [];
  bool reachable = false;
  bool authFailed = false; // wrong or missing secret
  Diagnosis? diagnosis; // why the mirror cannot be reached (null while all is fine)
  bool checked = false; // true after the first poll finished

  String primary = '';
  String secondary = '';
  bool autoCall = true;
  List<CallRecord> callLog = [];
  Escalation? escalation;

  /// Set by the home page from the app life cycle.
  bool foreground = true;

  final Set<String> _seen = {};
  // Alert ids that already had their call sequence (or were dismissed). Saved on the
  // phone so an app restart or a mirror switch does not start the calls again.
  final Set<String> _handled = {};
  Timer? _timer;
  Timer? _callTimer;
  int _tick = 0;
  int _fails = 0;
  bool _busy = false;
  bool _disposed = false;

  static const _kPrefKeys = [
    'contact_primary',
    'contact_secondary',
    'auto_call',
    'call_log',
    'handled_alerts',
    'cam',
  ];

  /// Removes everything this controller keeps on the phone (used when signing out,
  /// so the next account does not inherit the contacts and call history).
  static Future<void> clearLocalData() async {
    final p = await SharedPreferences.getInstance();
    for (final k in _kPrefKeys) {
      await p.remove(k);
    }
  }

  /// Cleans a phone number. Returns '' for empty input and null when it is not valid.
  static String? cleanPhone(String raw) {
    final s = raw.replaceAll(RegExp(r'[\s\-().]'), '');
    if (s.isEmpty) return '';
    return RegExp(r'^\+?\d{5,15}$').hasMatch(s) ? s : null;
  }

  // ---------- life cycle ----------
  Future<void> init() async {
    final p = await SharedPreferences.getInstance();
    primary = p.getString('contact_primary') ?? '';
    secondary = p.getString('contact_secondary') ?? '';
    autoCall = p.getBool('auto_call') ?? true;
    _handled.addAll(p.getStringList('handled_alerts') ?? const []);
    try {
      final raw = p.getString('call_log');
      if (raw != null) {
        callLog = (jsonDecode(raw) as List).map((e) => CallRecord.fromJson(e as Map<String, dynamic>)).toList();
      }
    } catch (_) {}
    if (_disposed) return;
    notifyListeners();
    start();
  }

  void start() {
    _timer?.cancel();
    poll();
    _timer = Timer.periodic(const Duration(seconds: 3), (_) => poll());
  }

  void stop() => _timer?.cancel();

  /// Called by the home page when the app goes to / returns from the background.
  void setForeground(bool v) {
    foreground = v;
    final e = escalation;
    // A retry was due while the app was away (most likely the person was on the phone
    // call). Do not dial over that call: ask first.
    if (v && e != null && e.backgroundSince != null && !e.finished && !_disposed) {
      _callTimer?.cancel();
      e.backgroundSince = null;
      e.state = 'confirm';
      e.text = 'You left the app during the call. Did they answer?';
      notifyListeners();
    }
  }

  @override
  void dispose() {
    _disposed = true;
    _timer?.cancel();
    _callTimer?.cancel();
    super.dispose();
  }

  void _markHandled(String id) {
    _handled.add(id);
    while (_handled.length > 100) {
      _handled.remove(_handled.first);
    }
    _persistHandled();
  }

  Future<void> _persistHandled() async {
    final p = await SharedPreferences.getInstance();
    await p.setStringList('handled_alerts', _handled.toList());
  }

  // ---------- polling ----------
  Future<void> poll() async {
    if (_busy || _disposed) return;
    _busy = true;
    try {
      if (kMirrorSecret.isEmpty) {
        // Nothing can work without the secret: say so instead of trying.
        authFailed = true;
        reachable = false;
        diagnosis = await diagnoseError(MirrorAuthException(), mirror);
        return;
      }
      final api = _api;
      final s = await api.status();
      final a = await api.alerts();
      status = s;
      alerts = a;
      reachable = true;
      authFailed = false;
      diagnosis = null;
      _fails = 0;
      if (_tick % 4 == 0) {
        try {
          events = await api.events();
        } catch (_) {}
      }
      _tick++;
      _onAlerts(a);
    } on MirrorAuthException catch (e) {
      authFailed = true;
      reachable = false;
      diagnosis = await diagnoseError(e, mirror);
    } catch (e) {
      _fails++;
      if (_fails >= 2) {
        reachable = false;
        authFailed = false;
        diagnosis = await diagnoseError(e, mirror);
      }
    } finally {
      checked = true;
      _busy = false;
      if (!_disposed) notifyListeners();
    }
  }

  /// True when the alert is too old for automatic calls. An alert with no timestamp
  /// (0) is treated as new: dropping a real emergency is worse than one extra call.
  /// [skew] is phone clock minus mirror clock, so a wrong mirror clock cannot make a
  /// fresh alert look old (or an old one look fresh).
  static bool isTooOld(SafetyAlert a, [DateTime? now, Duration skew = Duration.zero]) {
    if (a.timestamp <= 0) return false;
    return (now ?? DateTime.now()).subtract(skew).difference(a.time) > kAutoCallMaxAge;
  }

  void _onAlerts(List<SafetyAlert> a) {
    final fresh = a.where((x) => !_seen.contains(x.id)).toList();
    for (final x in a) {
      _seen.add(x.id);
    }
    if (fresh.any((x) => x.urgent)) {
      HapticFeedback.vibrate();
      SystemSound.play(SystemSoundType.alert);
    }

    // The alert was dismissed somewhere else (for example by the emergency contact).
    final e = escalation;
    if (e != null && !a.any((x) => x.id == e.alert.id)) _endEscalation();

    // A finished sequence (answered / failed / no contact) must not block a NEW alert.
    final cur = escalation;
    // While the screen says "no contact" and there is still no contact, do not restart the
    // sequence on every poll. Adding a contact (saveContacts) clears it and starts the calls.
    final waitingForContact = cur != null && cur.state == 'nocontact' && primary.isEmpty && secondary.isEmpty;
    if ((cur == null || cur.finished) && autoCall && !waitingForContact) {
      for (final x in a) {
        if (x.needsCall && !_handled.contains(x.id)) {
          if (isTooOld(x, null, MirrorApi.clockSkew)) {
            _markHandled(x.id); // shown in the list, but no automatic calls
            continue;
          }
          _startEscalation(x);
          break;
        }
      }
    }
  }

  // ---------- emergency calls ----------
  void _startEscalation(SafetyAlert a) {
    _callTimer?.cancel();
    _markHandled(a.id);
    escalation = Escalation(a);
    _nextCall();
  }

  void _endEscalation() {
    _callTimer?.cancel();
    escalation = null;
  }

  void _onRetryTimer() {
    final e = escalation;
    if (e == null || _disposed || e.finished) return;
    if (foreground) {
      _nextCall();
      return;
    }
    // The app is in the background (probably the phone call is still open): wait.
    e.backgroundSince ??= DateTime.now();
    if (DateTime.now().difference(e.backgroundSince!) >= _kMaxBackgroundWait) {
      e.backgroundSince = null;
      _nextCall();
      return;
    }
    _callTimer = Timer(const Duration(seconds: 5), _onRetryTimer);
  }

  Future<void> _nextCall() async {
    final e = escalation;
    if (e == null || _disposed) return;
    e.backgroundSince = null;
    String? phone;
    String who = '';
    int attempt = 1;
    if (e.primaryTries < 3 && primary.isNotEmpty) {
      e.primaryTries++;
      phone = primary;
      who = 'PRIMARY';
      attempt = e.primaryTries;
    } else if (!e.secondaryDone && secondary.isNotEmpty) {
      e.secondaryDone = true;
      phone = secondary;
      who = 'SECONDARY';
    }

    if (phone == null) {
      e.state = (primary.isEmpty && secondary.isEmpty) ? 'nocontact' : 'failed';
      e.text = e.state == 'nocontact'
          ? 'No emergency contact is set. Add one in Settings.'
          : 'No one answered. Please check on the person.';
      if (e.state == 'nocontact') {
        // Let this alert start the calls once a contact is added.
        _handled.remove(e.alert.id);
        _persistHandled();
      }
      notifyListeners();
      return;
    }

    e.state = 'waiting';
    e.nextAt = DateTime.now().add(_kRetryGap);
    e.text = who == 'PRIMARY' ? 'Called primary contact (attempt $attempt of 3).' : 'Called secondary contact.';
    notifyListeners();

    // Start the timer before dialing: dialing can take a while and must not delay the retry.
    _callTimer?.cancel();
    _callTimer = Timer(_kRetryGap, _onRetryTimer);
    final placed = await _dial(phone, who, attempt, e.alert);
    // Do not say "Called ..." when the phone refused (for example no call permission).
    if (!placed && !_disposed && escalation == e && !e.finished) {
      e.text = 'Could not place the call to the ${who.toLowerCase()} contact. '
          'Check the phone permission and the number.';
      notifyListeners();
    }
  }

  Future<bool> _dial(String phone, String who, int attempt, SafetyAlert a) async {
    bool ok = false;
    try {
      if (Platform.isAndroid) {
        ok = (await FlutterPhoneDirectCaller.callNumber(phone)) ?? false;
      } else {
        // iPhones do not allow silent calls: this opens the dialer and the user taps Call.
        ok = await launchUrl(Uri(scheme: 'tel', path: phone));
      }
    } catch (_) {
      ok = false;
    }
    await _record(who, phone, attempt, ok ? 'dialed' : 'could not dial', a.title);
    return ok;
  }

  Future<void> _record(String who, String phone, int attempt, String result, String incident) async {
    callLog.insert(0, CallRecord(DateTime.now().millisecondsSinceEpoch ~/ 1000, who, phone, attempt, result));
    if (callLog.length > 50) callLog = callLog.sublist(0, 50);
    final p = await SharedPreferences.getInstance();
    await p.setString('call_log', jsonEncode(callLog.map((c) => c.toJson()).toList()));
    try {
      await _api.logCall(target: who, phone: phone, attempt: attempt, result: result, incident: incident);
    } catch (_) {}
  }

  /// The person tapped "They answered": stop calling.
  void markAnswered() {
    final e = escalation;
    if (e == null) return;
    _callTimer?.cancel();
    e.backgroundSince = null;
    e.state = 'answered';
    e.text = 'Marked as answered. Calls stopped.';
    notifyListeners();
  }

  /// The person tapped "Call again" (after the call ended with no answer).
  void callAgain() {
    final e = escalation;
    if (e == null || e.finished) return;
    _callTimer?.cancel();
    _nextCall();
  }

  /// Call the primary (or secondary) contact right now, by hand.
  Future<void> callNow(SafetyAlert a) async {
    final phone = primary.isNotEmpty ? primary : secondary;
    if (phone.isEmpty) return;
    await _dial(phone, primary.isNotEmpty ? 'PRIMARY' : 'SECONDARY', 1, a);
  }

  // ---------- commands ----------
  Future<String?> _run(Future<void> Function() f) async {
    try {
      await f();
      await poll();
      return null;
    } catch (e) {
      return (await diagnoseError(e, mirror)).message;
    }
  }

  Future<String?> setSecurity(bool on) => _run(() => _api.setSecurity(on));
  Future<String?> setHomeAlone(bool on) => _run(() => _api.setHomeAlone(on));
  Future<String?> setCamera(bool on) => _run(() => _api.setCamera(on));
  Future<String?> setResolution(String p) => _run(() => _api.setResolution(p));
  Future<String?> setVoiceMuted(bool m) => _run(() => _api.setVoiceMuted(m));

  Future<String?> dismiss(SafetyAlert a) async {
    if (escalation?.alert.id == a.id) _endEscalation();
    _markHandled(a.id);
    return _run(() => _api.dismiss(a.id));
  }

  // ---------- settings ----------
  /// [p1] and [p2] must already be cleaned with [cleanPhone].
  Future<void> saveContacts(String p1, String p2, bool auto) async {
    primary = p1.trim();
    secondary = p2.trim();
    autoCall = auto;
    final p = await SharedPreferences.getInstance();
    await p.setString('contact_primary', primary);
    await p.setString('contact_secondary', secondary);
    await p.setBool('auto_call', autoCall);
    // A contact was added after "no contact": let the next poll start the calls.
    if (escalation?.state == 'nocontact' && (primary.isNotEmpty || secondary.isNotEmpty)) {
      escalation = null;
    }
    if (!_disposed) notifyListeners();
  }
}
