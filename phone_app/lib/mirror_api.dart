import 'dart:convert';
import 'dart:io' show HttpDate, SocketException;
import 'package:http/http.dart' as http;
import 'config.dart';
import 'models.dart';

class MirrorAuthException implements Exception {}

/// The mirror answered, but not with a normal reply (wrong status code, broken backend).
class MirrorHttpException implements Exception {
  final int statusCode;
  MirrorHttpException(this.statusCode);
  @override
  String toString() => 'HTTP $statusCode';
}

class ChatReply {
  final String reply;
  final String mode; // 'cloud' or 'local'
  const ChatReply(this.reply, this.mode);
}

class MirrorStatus {
  final bool cameraEnabled;
  final bool securityMode;
  final bool homeAloneMode;
  final bool voiceMuted;
  final double? cpuTempC;
  final String aiMode;
  final String resolution;
  final int? wifiDbm;

  const MirrorStatus({
    required this.cameraEnabled,
    required this.securityMode,
    required this.homeAloneMode,
    required this.voiceMuted,
    required this.cpuTempC,
    required this.aiMode,
    required this.resolution,
    required this.wifiDbm,
  });

  factory MirrorStatus.fromJson(Map<String, dynamic> j) => MirrorStatus(
        cameraEnabled: j['camera_enabled'] != false,
        securityMode: j['security_mode'] == true,
        homeAloneMode: j['home_alone_mode'] == true,
        voiceMuted: j['voice_muted'] == true,
        cpuTempC: (j['cpu_temperature_c'] as num?)?.toDouble(),
        aiMode: (j['ai_routing_mode'] ?? 'local').toString(),
        resolution: (j['resolution'] ?? '720p').toString(),
        wifiDbm: (j['wifi_signal_dbm'] as num?)?.toInt(),
      );
}

class SafetyAlert {
  final String id;
  final String type;
  final String priority;
  final String title;
  final String message;
  final int timestamp;
  final String? thumbnailB64;

  const SafetyAlert({
    required this.id,
    required this.type,
    required this.priority,
    required this.title,
    required this.message,
    required this.timestamp,
    this.thumbnailB64,
  });

  factory SafetyAlert.fromJson(Map<String, dynamic> j) => SafetyAlert(
        id: (j['id'] ?? '').toString(),
        type: (j['type'] ?? '').toString(),
        priority: (j['priority'] ?? 'LOW').toString(),
        title: (j['title'] ?? 'Alert').toString(),
        message: (j['message'] ?? '').toString(),
        timestamp: (j['timestamp'] as num?)?.toInt() ?? 0,
        thumbnailB64: j['thumbnail']?.toString(),
      );

  DateTime get time => DateTime.fromMillisecondsSinceEpoch(timestamp * 1000);

  /// These two mean a person may need help: the phone should start calling.
  bool get needsCall => type == 'FALL_DETECTED' || type == 'MOTIONLESS_ESCALATION';
  bool get urgent => priority == 'CRITICAL' || priority == 'HIGH';
}

/// Talks to motion_stream_server.py on the mirror.
class MirrorApi {
  final Mirror mirror;
  MirrorApi(this.mirror);

  Uri _u(String path) => Uri.parse('http://${mirror.address}:${mirror.port}$path');
  Map<String, String> get _h => {'X-Mirror-Secret': kMirrorSecret, 'Content-Type': 'application/json'};
  // Mobile data and the Tailscale tunnel are slower than home Wi-Fi.
  static const _timeout = Duration(seconds: 8);

  /// Phone clock minus mirror clock, measured from the mirror's HTTP `Date` header
  /// (1 second precision). Alert timestamps come from the mirror's clock, so the app
  /// uses this to judge their age correctly even when the two clocks disagree.
  static Duration clockSkew = Duration.zero;

  static void _noteServerDate(http.Response r) {
    final d = r.headers['date'];
    if (d == null) return;
    try {
      clockSkew = DateTime.now().toUtc().difference(HttpDate.parse(d));
    } catch (_) {}
  }

  Map<String, dynamic> _decode(http.Response r) {
    _noteServerDate(r);
    if (r.statusCode == 401) throw MirrorAuthException();
    if (r.statusCode != 200) throw MirrorHttpException(r.statusCode);
    // Some commands answer 200 with an empty body: that is still a success.
    if (r.body.trim().isEmpty) return <String, dynamic>{};
    return jsonDecode(r.body) as Map<String, dynamic>;
  }

  // Reading is safe to repeat: one quick retry when the connection is dropped
  // (common on mobile data). A timeout is not retried, the 3-second poll tries again anyway.
  Future<Map<String, dynamic>> _get(String path) async {
    for (var attempt = 0;; attempt++) {
      try {
        return _decode(await http.get(_u(path), headers: _h).timeout(_timeout));
      } on SocketException {
        if (attempt >= 1) rethrow;
      } on http.ClientException {
        if (attempt >= 1) rethrow;
      }
      await Future<void>.delayed(const Duration(milliseconds: 400));
    }
  }

  Future<Map<String, dynamic>> _post(String path, Map<String, dynamic> body) async =>
      _decode(await http.post(_u(path), headers: _h, body: jsonEncode(body)).timeout(_timeout));

  Future<MirrorStatus> status() async => MirrorStatus.fromJson(await _get('/status'));

  Future<List<SafetyAlert>> alerts() async {
    final j = await _get('/alerts');
    return ((j['active_alerts'] as List?) ?? [])
        .map((e) => SafetyAlert.fromJson(e as Map<String, dynamic>))
        .toList()
      ..sort((a, b) => b.timestamp.compareTo(a.timestamp));
  }

  Future<List<SafetyAlert>> events({int limit = 40}) async {
    final j = await _get('/events?limit=$limit');
    return ((j['events'] as List?) ?? [])
        .map((e) => SafetyAlert.fromJson(e as Map<String, dynamic>))
        .toList()
      ..sort((a, b) => b.timestamp.compareTo(a.timestamp));
  }

  Future<void> setSecurity(bool on) => _post('/mode/security', {'enabled': on});
  Future<void> setHomeAlone(bool on) => _post('/mode/home_alone', {'enabled': on});
  Future<void> setCamera(bool on) => _post('/camera/toggle', {'enabled': on});
  Future<void> setResolution(String preset) => _post('/settings/resolution', {'preset': preset});
  Future<void> setVoiceMuted(bool muted) => _post('/settings/voice_mute', {'muted': muted});
  Future<void> dismiss(String? id) => _post('/alerts/dismiss', {'id': id});

  // ---------- tasks and reminders kept on the mirror (said to Vision, or added in this app) ----------
  Future<Map<String, dynamic>> agenda() => _get('/agenda');
  Future<void> addReminder(String title, DateTime when) => _post('/agenda/reminder', {
        'title': title,
        'due_at': '${when.year.toString().padLeft(4, '0')}-${when.month.toString().padLeft(2, '0')}-'
            '${when.day.toString().padLeft(2, '0')}T${when.hour.toString().padLeft(2, '0')}:'
            '${when.minute.toString().padLeft(2, '0')}',
      });
  Future<void> deleteReminder(int id) => _post('/agenda/reminder/delete', {'id': id});
  Future<void> doneReminder(int id) => _post('/agenda/reminder/done', {'id': id});

  /// Google Tasks changed from the phone: the mirror reads Google now, not in 30 seconds.
  Future<void> syncGoogle() => _post('/agenda/sync', {});

  /// The Google account connected in the app becomes the mirror's (its tasks and calendar).
  Future<Map<String, dynamic>> linkGoogle(Map<String, dynamic> link) => _post('/agenda/google/link', link);
  Future<void> unlinkGoogle() => _post('/agenda/google/unlink', {});

  /// A fresh Google token from the mirror (it keeps the sign-in), instead of opening the Google page.
  Future<Map<String, dynamic>> googleToken() => _post('/agenda/google/token', {});
  Future<void> addTask(String title, {DateTime? due}) => _post('/agenda/task', {
        'title': title,
        if (due != null)
          'date': '${due.year.toString().padLeft(4, '0')}-${due.month.toString().padLeft(2, '0')}'
              '-${due.day.toString().padLeft(2, '0')}',
      });
  Future<void> completeTask(int id) => _post('/agenda/task/done', {'id': id});
  Future<void> deleteTask(int id) => _post('/agenda/task/delete', {'id': id});

  /// Tells the mirror this phone's push-notification token (see lib/push.dart).
  Future<void> registerDevice(String token, {bool enabled = true}) =>
      _post('/devices', {'token': token, 'enabled': enabled});

  /// Chat with the mirror's assistant. The AI key stays on the mirror, never in the app.
  Future<ChatReply> chat(String message, List<Map<String, String>> history) async {
    final r = await http
        .post(_u('/chat'), headers: _h, body: jsonEncode({'message': message, 'history': history}))
        .timeout(const Duration(seconds: 30));
    final j = _decode(r);
    return ChatReply((j['reply'] ?? '').toString(), (j['mode'] ?? 'local').toString());
  }

  Future<void> logCall({
    required String target,
    required String phone,
    required int attempt,
    required String result,
    required String incident,
  }) =>
      _post('/calls/log', {
        'target': target,
        'phone': phone,
        'attempt': attempt,
        'result': result,
        'incident': incident,
      });
}
