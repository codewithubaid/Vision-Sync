import 'dart:convert';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'package:supabase_flutter/supabase_flutter.dart';
import 'config.dart';
import 'models.dart';

class GoogleAuthException implements Exception {}

/// Starts Google sign-in through Supabase and asks for Calendar + Tasks access.
Future<bool> connectGoogle({bool consent = true}) async {
  final p = await SharedPreferences.getInstance();
  await p.remove('g_disabled');
  return supabase.auth.signInWithOAuth(
    OAuthProvider.google,
    redirectTo: kRedirect,
    scopes: kGoogleScopes,
    queryParams: {'access_type': 'offline', if (consent) 'prompt': 'consent'},
  );
}

class GoogleApi {
  static const _tokKey = 'g_token';
  static const _expKey = 'g_token_exp';
  static const _disKey = 'g_disabled';
  static const _reKey = 'g_reauth';
  static const _deadKey = 'g_token_dead';

  // Google access tokens last about 1 hour.
  static Future<void> saveToken(String t) async {
    final p = await SharedPreferences.getInstance();
    // The same token can arrive again (for example when a stored session is restored).
    // Do not give an old or rejected token a new 55 minutes.
    if (t == p.getString(_tokKey) || t == p.getString(_deadKey)) return;
    await p.setString(_tokKey, t);
    await p.setInt(_expKey, DateTime.now().add(const Duration(minutes: 55)).millisecondsSinceEpoch);
  }

  static Future<String?> token() async {
    final p = await SharedPreferences.getInstance();
    final t = p.getString(_tokKey);
    final exp = p.getInt(_expKey) ?? 0;
    return (t != null && exp > DateTime.now().millisecondsSinceEpoch) ? t : null;
  }

  static Future<void> clear({bool disable = false}) async {
    final p = await SharedPreferences.getInstance();
    final old = p.getString(_tokKey);
    if (old != null) await p.setString(_deadKey, old);
    await p.remove(_tokKey);
    await p.remove(_expKey);
    if (disable) await p.setBool(_disKey, true);
  }

  static Future<bool> isDisabled() async =>
      (await SharedPreferences.getInstance()).getBool(_disKey) ?? false;

  // ---- The Google sign-in given to the mirror, so it syncs the same account ----
  static const _linkKey = 'g_link_refresh';
  static const _linkAccessKey = 'g_link_access';

  /// Kept on this phone only until the mirror has it.
  static Future<void> saveMirrorLink(String refresh, String? access) async {
    final p = await SharedPreferences.getInstance();
    await p.setString(_linkKey, refresh);
    if (access == null) {
      await p.remove(_linkAccessKey);
    } else {
      await p.setString(_linkAccessKey, access);
    }
  }

  static Future<Map<String, dynamic>?> pendingMirrorLink() async {
    final p = await SharedPreferences.getInstance();
    final refresh = p.getString(_linkKey);
    if (refresh == null || refresh.isEmpty) return null;
    final access = p.getString(_linkAccessKey);
    return <String, dynamic>{'refresh_token': refresh, if (access != null) 'access_token': access};
  }

  static Future<void> clearMirrorLink() async {
    final p = await SharedPreferences.getInstance();
    await p.remove(_linkKey);
    await p.remove(_linkAccessKey);
  }

  /// At most one silent re-login every 5 minutes.
  static Future<bool> shouldReauth() async {
    final p = await SharedPreferences.getInstance();
    final last = p.getInt(_reKey) ?? 0;
    final now = DateTime.now().millisecondsSinceEpoch;
    if (now - last < 300000) return false;
    await p.setInt(_reKey, now);
    return true;
  }

  static Map<String, String> _h(String t) => {'Authorization': 'Bearer $t', 'Content-Type': 'application/json'};

  static void _check(http.Response r) {
    if (r.statusCode == 401) throw GoogleAuthException();
    // 403 is also used for quota and "API not enabled" errors: only a scope / token
    // problem should throw the user back to the Connect screen.
    if (r.statusCode == 403 && RegExp(r'insufficient|scope|invalid_token', caseSensitive: false).hasMatch(r.body)) {
      throw GoogleAuthException();
    }
    if (r.statusCode >= 400) throw Exception('Google error ${r.statusCode}');
  }

  static String _2(int n) => n.toString().padLeft(2, '0');

  // ---- Tasks ----
  static const _tasks = 'https://tasks.googleapis.com/tasks/v1/lists/@default/tasks';

  static Future<List<GTask>> listTasks(String t) async {
    final r = await http.get(Uri.parse('$_tasks?showCompleted=true&showHidden=true&maxResults=100'), headers: _h(t));
    _check(r);
    final items = (jsonDecode(r.body)['items'] as List?) ?? [];
    return items
        .map((e) => GTask.fromJson(e as Map<String, dynamic>))
        .where((x) => x.title.trim().isNotEmpty)
        .toList();
  }

  static Future<void> addTask(String t, String title, DateTime? due) async {
    final body = <String, dynamic>{'title': title};
    if (due != null) body['due'] = '${due.year.toString().padLeft(4, '0')}-${_2(due.month)}-${_2(due.day)}T00:00:00.000Z';
    _check(await http.post(Uri.parse(_tasks), headers: _h(t), body: jsonEncode(body)));
  }

  static Future<void> setDone(String t, String id, bool done) async {
    final body = done ? {'status': 'completed'} : {'status': 'needsAction', 'completed': null};
    _check(await http.patch(Uri.parse('$_tasks/$id'), headers: _h(t), body: jsonEncode(body)));
  }

  static Future<void> deleteTask(String t, String id) async {
    _check(await http.delete(Uri.parse('$_tasks/$id'), headers: _h(t)));
  }

  // ---- Calendar (today + next 2 days) ----
  static Future<List<CalEvent>> listEvents(String t) async {
    final n = DateTime.now();
    final from = DateTime(n.year, n.month, n.day);
    final to = DateTime(n.year, n.month, n.day + 3); // not +72 hours: daylight saving
    final uri = Uri.https('www.googleapis.com', '/calendar/v3/calendars/primary/events', {
      'singleEvents': 'true',
      'orderBy': 'startTime',
      'maxResults': '50',
      'timeMin': from.toUtc().toIso8601String(),
      'timeMax': to.toUtc().toIso8601String(),
    });
    final r = await http.get(uri, headers: _h(t));
    _check(r);
    final items = (jsonDecode(r.body)['items'] as List?) ?? [];
    return items
        .where((e) => (e as Map<String, dynamic>)['start'] != null)
        .map((e) => CalEvent.fromJson(e as Map<String, dynamic>))
        .toList();
  }
}
