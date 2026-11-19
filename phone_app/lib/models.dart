import 'dart:convert';
import 'package:shared_preferences/shared_preferences.dart';

const _months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const _days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

// Calendar day in UTC, so a 23- or 25-hour day (daylight saving) still counts as one day.
DateTime _dayOnly(DateTime d) => DateTime.utc(d.year, d.month, d.day);

String dueLabel(DateTime? d) {
  if (d == null) return 'No date';
  final diff = _dayOnly(d).difference(_dayOnly(DateTime.now())).inDays;
  final short = '${d.day} ${_months[d.month - 1]}';
  if (diff == 0) return 'Today';
  if (diff == 1) return 'Tomorrow';
  if (diff < 0) return 'Overdue · $short';
  return '${_days[d.weekday - 1]} $short';
}

String dayLabel(DateTime d) {
  final diff = _dayOnly(d).difference(_dayOnly(DateTime.now())).inDays;
  if (diff <= 0) return 'Today';
  if (diff == 1) return 'Tomorrow';
  return '${_days[d.weekday - 1]} ${d.day} ${_months[d.month - 1]}';
}

String timeLabel(DateTime d) {
  final h = d.hour % 12 == 0 ? 12 : d.hour % 12;
  final m = d.minute.toString().padLeft(2, '0');
  return '$h:$m ${d.hour < 12 ? 'AM' : 'PM'}';
}

/// Parses a timestamp from the database. A value without a time zone (for example a
/// `timestamp` column) is read as UTC instead of the phone's local time.
DateTime? parseDbTime(Object? v) {
  if (v == null) return null;
  var s = v.toString().trim();
  if (s.isEmpty) return null;
  final hasTime = RegExp(r'\d{2}:\d{2}').hasMatch(s);
  final hasZone = RegExp(r'(Z|[+-]\d{2}(:?\d{2})?)$').hasMatch(s);
  if (hasTime && !hasZone) s = '${s.replaceFirst(' ', 'T')}Z';
  return DateTime.tryParse(s);
}

class Mirror {
  final String id; // a String so both numeric and UUID ids work
  final String name;
  final String ip; // the mirror's home-network (LAN) address. Only works on that Wi-Fi.
  final int port;
  final int? signal;
  final DateTime? lastSeen;
  // Tailscale IP (100.x.x.x) or MagicDNS name. Works from any network once both
  // devices are on the same tailnet. Preferred over [ip] whenever it is set.
  final String? tailscaleHost;

  const Mirror({
    required this.id,
    required this.name,
    required this.ip,
    required this.port,
    this.signal,
    this.lastSeen,
    this.tailscaleHost,
  });

  bool get usingTailscale => (tailscaleHost ?? '').isNotEmpty;

  /// The address every request and the video stream use.
  String get address => usingTailscale ? tailscaleHost! : ip;

  Mirror withHost(String? host) => Mirror(
        id: id,
        name: name,
        ip: ip,
        port: port,
        signal: signal,
        lastSeen: lastSeen,
        tailscaleHost: (host ?? '').isEmpty ? null : host,
      );

  factory Mirror.fromMap(Map<String, dynamic> m) {
    final ip = (m['ip'] ?? '').toString();
    final name = (m['name'] ?? '').toString();
    return Mirror(
      id: (m['id'] ?? '').toString(),
      name: name.isNotEmpty ? name : (ip.isNotEmpty ? ip : 'Mirror'),
      ip: ip,
      port: int.tryParse((m['port'] ?? '').toString()) ?? 8080,
      signal: (m['signal'] as num?)?.toInt(),
      lastSeen: parseDbTime(m['last_seen']),
      tailscaleHost: _hostOrNull(m['tailscale_host'] ?? m['tailscale_ip']),
    );
  }

  static String? _hostOrNull(Object? v) {
    final s = (v ?? '').toString().trim();
    return s.isEmpty ? null : s;
  }

  Map<String, dynamic> toMap() => {
        'id': id,
        'name': name,
        'ip': ip,
        'port': port,
        'signal': signal,
        'last_seen': lastSeen?.toIso8601String(),
        'tailscale_host': tailscaleHost,
      };

  /// The mirror writes `last_seen` every ~30s. No value yet = assume online.
  bool get online => lastSeen == null || DateTime.now().difference(lastSeen!).inSeconds < 120;

  static const _key = 'saved_mirror';

  static Future<Mirror?> loadSaved() async {
    final p = await SharedPreferences.getInstance();
    final s = p.getString(_key);
    if (s == null) return null;
    try {
      return Mirror.fromMap(jsonDecode(s) as Map<String, dynamic>);
    } catch (_) {
      return null;
    }
  }

  Future<void> save() async {
    final p = await SharedPreferences.getInstance();
    await p.setString(_key, jsonEncode(toMap()));
  }

  static Future<void> forget() async {
    final p = await SharedPreferences.getInstance();
    await p.remove(_key);
  }
}

class GTask {
  final String id;
  final String title;
  bool done;
  final DateTime? due;

  GTask({required this.id, required this.title, required this.done, this.due});

  factory GTask.fromJson(Map<String, dynamic> j) {
    final d = j['due'] as String?;
    return GTask(
      id: j['id'] as String,
      title: (j['title'] ?? '') as String,
      done: j['status'] == 'completed',
      due: (d == null || d.length < 10) ? null : DateTime.tryParse(d.substring(0, 10)),
    );
  }
}

class CalEvent {
  final String title;
  final DateTime start;
  final DateTime? end;
  final bool allDay;
  final String location;
  final String id; // Google's id for the event

  CalEvent({
    this.id = '',
    required this.title,
    required this.start,
    required this.end,
    required this.allDay,
    required this.location,
  });

  factory CalEvent.fromJson(Map<String, dynamic> j) {
    final s = j['start'] as Map<String, dynamic>;
    final e = (j['end'] as Map<String, dynamic>?) ?? <String, dynamic>{};
    final allDay = s['dateTime'] == null;
    return CalEvent(
      title: ((j['summary'] ?? '(No title)') as String),
      start: allDay ? DateTime.parse(s['date'] as String) : DateTime.parse(s['dateTime'] as String).toLocal(),
      end: (allDay || e['dateTime'] == null) ? null : DateTime.parse(e['dateTime'] as String).toLocal(),
      allDay: allDay,
      location: (j['location'] ?? '') as String,
      id: (j['id'] ?? '').toString(),
    );
  }

  String get timeText => allDay ? 'All day' : (end == null ? timeLabel(start) : '${timeLabel(start)} – ${timeLabel(end!)}');
}
