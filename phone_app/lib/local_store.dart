import 'dart:convert';
import 'package:shared_preferences/shared_preferences.dart';
import 'models.dart';

/// Tasks and reminders the user types in by hand. They are saved on the phone, so they
/// work even when Google is not connected.
const kLocalPrefix = 'local_';

bool isLocalTask(GTask t) => t.id.startsWith(kLocalPrefix);

class Reminder {
  final String id;
  final String title;
  final DateTime when; // phone's local time
  bool done;

  Reminder({required this.id, required this.title, required this.when, this.done = false});

  Map<String, dynamic> toJson() => {
        'id': id,
        'title': title,
        'when': when.toIso8601String(), // no time zone: read back as local time
        'done': done,
      };

  static Reminder? fromJson(Map<String, dynamic> j) {
    final when = DateTime.tryParse((j['when'] ?? '').toString());
    final id = (j['id'] ?? '').toString();
    if (when == null || id.isEmpty) return null;
    return Reminder(
      id: id,
      title: (j['title'] ?? '').toString(),
      when: when.toLocal(),
      done: j['done'] == true,
    );
  }
}

Map<String, dynamic> localTaskToJson(GTask t) => {
      'id': t.id,
      'title': t.title,
      'done': t.done,
      'due': t.due?.toIso8601String(),
    };

GTask? localTaskFromJson(Map<String, dynamic> j) {
  final id = (j['id'] ?? '').toString();
  if (id.isEmpty) return null;
  return GTask(
    id: id,
    title: (j['title'] ?? '').toString(),
    done: j['done'] == true,
    due: DateTime.tryParse((j['due'] ?? '').toString()),
  );
}

class LocalStore {
  static const _tasksKey = 'local_tasks';
  static const _remindersKey = 'local_reminders';

  static String newId() => '$kLocalPrefix${DateTime.now().microsecondsSinceEpoch}';

  static Future<List<GTask>> loadTasks() async {
    final p = await SharedPreferences.getInstance();
    try {
      final raw = jsonDecode(p.getString(_tasksKey) ?? '[]') as List;
      return raw.map((e) => localTaskFromJson(e as Map<String, dynamic>)).whereType<GTask>().toList();
    } catch (_) {
      return [];
    }
  }

  static Future<void> saveTasks(List<GTask> tasks) async {
    final p = await SharedPreferences.getInstance();
    await p.setString(_tasksKey, jsonEncode(tasks.where(isLocalTask).map(localTaskToJson).toList()));
  }

  static Future<List<Reminder>> loadReminders() async {
    final p = await SharedPreferences.getInstance();
    try {
      final raw = jsonDecode(p.getString(_remindersKey) ?? '[]') as List;
      final out = raw.map((e) => Reminder.fromJson(e as Map<String, dynamic>)).whereType<Reminder>().toList();
      out.sort((a, b) => a.when.compareTo(b.when));
      return out;
    } catch (_) {
      return [];
    }
  }

  static Future<void> saveReminders(List<Reminder> items) async {
    final p = await SharedPreferences.getInstance();
    await p.setString(_remindersKey, jsonEncode(items.map((r) => r.toJson()).toList()));
  }

  /// Called on sign-out: these items belong to the account that was signed in.
  static Future<void> clear() async {
    final p = await SharedPreferences.getInstance();
    await p.remove(_tasksKey);
    await p.remove(_remindersKey);
  }
}
