import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:glass_connect/connection.dart';
import 'package:glass_connect/local_store.dart';
import 'package:glass_connect/mirror_api.dart';
import 'package:glass_connect/models.dart';
import 'package:glass_connect/safety_controller.dart';

// These tests cover the pure logic. The app itself needs Supabase, so it is not
// pumped here.
void main() {
  group('labels', () {
    test('dueLabel', () {
      final now = DateTime.now();
      expect(dueLabel(null), 'No date');
      expect(dueLabel(now), 'Today');
      expect(dueLabel(now.add(const Duration(days: 1))), 'Tomorrow');
      expect(dueLabel(now.subtract(const Duration(days: 3))), startsWith('Overdue'));
    });

    test('timeLabel uses a 12-hour clock', () {
      expect(timeLabel(DateTime(2026, 1, 1, 0, 5)), '12:05 AM');
      expect(timeLabel(DateTime(2026, 1, 1, 12, 0)), '12:00 PM');
      expect(timeLabel(DateTime(2026, 1, 1, 13, 30)), '1:30 PM');
    });
  });

  group('Mirror', () {
    test('reads numeric and UUID ids, with defaults', () {
      final a = Mirror.fromMap({'id': 5, 'ip': '192.168.1.20'});
      expect(a.id, '5');
      expect(a.port, 8080);
      expect(a.name, '192.168.1.20'); // no name -> falls back to the ip

      final b = Mirror.fromMap({'id': '3f2c-uuid', 'name': 'Hall', 'ip': '10.0.0.2', 'port': 9000});
      expect(b.id, '3f2c-uuid');
      expect(b.name, 'Hall');
      expect(b.port, 9000);
    });

    test('online', () {
      const fresh = Mirror(id: '1', name: 'm', ip: 'x', port: 1);
      expect(fresh.online, isTrue); // no heartbeat yet = assume online
      final old = Mirror(id: '1', name: 'm', ip: 'x', port: 1, lastSeen: DateTime.now().subtract(const Duration(minutes: 10)));
      expect(old.online, isFalse);
    });

    test('toMap / fromMap round trip keeps the id', () {
      const m = Mirror(id: 'abc', name: 'n', ip: '1.2.3.4', port: 8080, signal: -60);
      final back = Mirror.fromMap(m.toMap());
      expect(back.id, 'abc');
      expect(back.signal, -60);
    });
  });

  group('parseDbTime', () {
    test('a time without a zone is read as UTC', () {
      final t = parseDbTime('2026-11-09T12:00:00')!;
      expect(t.isUtc, isTrue);
      expect(t.hour, 12);
      expect(parseDbTime('2026-11-09 12:00:00')!.isUtc, isTrue);
    });

    test('a time with a zone keeps its meaning', () {
      expect(parseDbTime('2026-11-09T12:00:00+05:00')!.toUtc().hour, 7);
      expect(parseDbTime('2026-11-09T12:00:00Z')!.hour, 12);
    });

    test('empty values', () {
      expect(parseDbTime(null), isNull);
      expect(parseDbTime(''), isNull);
    });
  });

  group('Google models', () {
    test('GTask', () {
      final t = GTask.fromJson({'id': '1', 'title': 'Milk', 'status': 'completed', 'due': '2026-11-12T00:00:00.000Z'});
      expect(t.done, isTrue);
      expect(t.due!.day, 12);
    });

    test('all-day CalEvent', () {
      final e = CalEvent.fromJson({
        'summary': 'Holiday',
        'start': {'date': '2026-11-12'},
        'end': {'date': '2026-11-13'},
      });
      expect(e.allDay, isTrue);
      expect(e.timeText, 'All day');
    });
  });

  group('emergency contacts', () {
    test('cleanPhone', () {
      expect(SafetyController.cleanPhone('+92 300-1234567'), '+923001234567');
      expect(SafetyController.cleanPhone('  '), '');
      expect(SafetyController.cleanPhone('abc'), isNull);
      expect(SafetyController.cleanPhone('12'), isNull);
    });

    test('old alerts do not start automatic calls', () {
      final now = DateTime(2026, 11, 11, 12, 0);
      int ts(Duration ago) => now.subtract(ago).millisecondsSinceEpoch ~/ 1000;
      SafetyAlert a(int t) =>
          SafetyAlert(id: 'x', type: 'FALL_DETECTED', priority: 'CRITICAL', title: 't', message: 'm', timestamp: t);
      expect(SafetyController.isTooOld(a(ts(const Duration(minutes: 2))), now), isFalse);
      expect(SafetyController.isTooOld(a(ts(const Duration(hours: 3))), now), isTrue);
      expect(SafetyController.isTooOld(a(0), now), isFalse); // no timestamp = treat as new
    });

    test('a wrong mirror clock does not hide a fresh alert', () {
      final now = DateTime(2026, 11, 11, 12, 0);
      // Mirror clock is 1 hour behind the phone: skew = +1 h.
      final mirrorNow = now.subtract(const Duration(hours: 1));
      final ts = mirrorNow.subtract(const Duration(minutes: 1)).millisecondsSinceEpoch ~/ 1000;
      final alert = SafetyAlert(id: 'x', type: 'FALL_DETECTED', priority: 'CRITICAL', title: 't', message: 'm', timestamp: ts);
      expect(SafetyController.isTooOld(alert, now), isTrue); // without correction it looks old
      expect(SafetyController.isTooOld(alert, now, const Duration(hours: 1)), isFalse);
    });

    test('alerts that need a call', () {
      const fall = SafetyAlert(id: '1', type: 'FALL_DETECTED', priority: 'CRITICAL', title: 't', message: 'm', timestamp: 0);
      const info = SafetyAlert(id: '2', type: 'SECURITY_ALERT', priority: 'LOW', title: 't', message: 'm', timestamp: 0);
      expect(fall.needsCall, isTrue);
      expect(fall.urgent, isTrue);
      expect(info.needsCall, isFalse);
    });
  });

  group('Tailscale address', () {
    test('address prefers the Tailscale host over the LAN ip', () {
      const lan = Mirror(id: '1', name: 'm', ip: '192.168.1.20', port: 8080);
      expect(lan.address, '192.168.1.20');
      expect(lan.usingTailscale, isFalse);
      final ts = lan.withHost('100.101.102.103');
      expect(ts.address, '100.101.102.103');
      expect(ts.usingTailscale, isTrue);
      expect(ts.withHost('').address, '192.168.1.20'); // cleared
      expect(Mirror.fromMap({'id': 1, 'ip': '10.0.0.2', 'tailscale_host': 'mirror.tail1.ts.net'}).address, 'mirror.tail1.ts.net');
      expect(Mirror.fromMap({'id': 1, 'ip': '10.0.0.2', 'tailscale_host': '  '}).address, '10.0.0.2');
    });

    test('isTailscaleIp only accepts 100.64.0.0/10', () {
      expect(isTailscaleIp('100.64.0.1'), isTrue);
      expect(isTailscaleIp('100.127.255.254'), isTrue);
      expect(isTailscaleIp('100.63.0.1'), isFalse);
      expect(isTailscaleIp('100.128.0.1'), isFalse);
      expect(isTailscaleIp('192.168.1.20'), isFalse);
    });

    test('validateHost', () {
      expect(validateHost(' 100.101.102.103 '), '100.101.102.103');
      expect(validateHost('http://Mirror.Tail1.ts.net'), 'mirror.tail1.ts.net');
      expect(validateHost('glass-mirror'), 'glass-mirror');
      expect(validateHost(''), '');
      expect(validateHost('100.101.102.103:8080'), isNull);
      expect(validateHost('999.1.1.1'), isNull);
      expect(validateHost('bad host'), isNull);
      expect(validateHost('a/b'), isNull);
    });
  });

  group('connection diagnosis', () {
    const ts = Mirror(id: '1', name: 'm', ip: '192.168.1.20', port: 8080, tailscaleHost: '100.101.102.103');
    final stale = Mirror(
        id: '1', name: 'm', ip: 'x', port: 1, tailscaleHost: '100.101.102.103', lastSeen: DateTime.now().subtract(const Duration(hours: 1)));
    final net = TimeoutException('t');

    test('missing secret', () async {
      expect((await diagnoseError(net, ts, secret: '')).issue, ConnIssue.noSecret);
    });
    test('wrong secret', () async {
      expect((await diagnoseError(MirrorAuthException(), ts, secret: 's')).issue, ConnIssue.authFailed);
    });
    test('server answers badly', () async {
      expect((await diagnoseError(MirrorHttpException(500), ts, secret: 's')).issue, ConnIssue.serverUnavailable);
      expect((await diagnoseError(const FormatException('x'), ts, secret: 's')).issue, ConnIssue.serverUnavailable);
    });
    test('phone-side causes come before an old last-seen', () async {
      expect((await diagnoseError(net, stale, secret: 's', tailscaleOnPhone: false)).issue, ConnIssue.tailscaleOff);
      expect((await diagnoseError(net, stale, secret: 's', tailscaleOnPhone: true)).issue, ConnIssue.mirrorOffline);
    });
    test('tailscale off on the phone', () async {
      expect((await diagnoseError(net, ts, secret: 's', tailscaleOnPhone: false)).issue, ConnIssue.tailscaleOff);
    });
    test('tailscale on, mirror silent', () async {
      expect((await diagnoseError(net, ts, secret: 's', tailscaleOnPhone: true)).issue, ConnIssue.unreachable);
    });
    test('only a LAN address is known', () async {
      const lan = Mirror(id: '1', name: 'm', ip: '192.168.1.20', port: 8080);
      expect((await diagnoseError(net, lan, secret: 's', tailscaleOnPhone: true)).issue, ConnIssue.lanOnly);
    });
    test('no message mentions the same Wi-Fi', () async {
      final d = await diagnoseError(net, ts, secret: 's', tailscaleOnPhone: true);
      expect(d.message.toLowerCase(), isNot(contains('same wi-fi')));
    });
  });

  group('manual tasks and reminders', () {
    test('a reminder survives the JSON round trip', () {
      final r = Reminder(id: 'local_1', title: 'Call mum', when: DateTime(2026, 11, 14, 15, 30));
      final back = Reminder.fromJson(r.toJson())!;
      expect(back.title, 'Call mum');
      expect(back.when, DateTime(2026, 11, 14, 15, 30));
      expect(back.done, isFalse);
    });

    test('a broken reminder is skipped, not a crash', () {
      expect(Reminder.fromJson({'id': 'x', 'when': 'nope'}), isNull);
    });

    test('a hand-typed task is recognised and round-trips', () {
      final t = GTask(id: 'local_9', title: 'Buy milk', done: true, due: DateTime(2026, 11, 20));
      expect(isLocalTask(t), isTrue);
      final back = localTaskFromJson(localTaskToJson(t))!;
      expect(back.title, 'Buy milk');
      expect(back.done, isTrue);
      expect(back.due, DateTime(2026, 11, 20));
      expect(isLocalTask(GTask(id: 'abc123', title: 'g', done: false)), isFalse);
    });
  });
}
