import 'dart:convert';
import 'package:flutter/material.dart';
import 'connection.dart';
import 'mirror_api.dart';
import 'models.dart';
import 'safety_controller.dart';
import 'theme.dart';

Color _priorityColor(String p) {
  switch (p) {
    case 'CRITICAL':
    case 'HIGH':
      return kRed;
    case 'MEDIUM':
      return kAmber;
    default:
      return kMuted;
  }
}

IconData _typeIcon(String t) {
  switch (t) {
    case 'SECURITY_ALERT':
      return Icons.shield;
    case 'FALL_DETECTED':
    case 'FALL_COUNTDOWN':
    case 'FALL_RECOVERED':
      return Icons.personal_injury;
    case 'MOTIONLESS_PROMPT':
    case 'MOTIONLESS_ESCALATION':
      return Icons.accessibility_new;
    case 'CAMERA_OFFLINE':
      return Icons.videocam_off;
    case 'CALL_ATTEMPT':
      return Icons.call;
    default:
      return Icons.info_outline;
  }
}

String _when(DateTime t) => '${dayLabel(t)} ${timeLabel(t)}';

/// The Alerts tab: safety modes, active alerts, emergency calls and recent events.
class SafetyPage extends StatelessWidget {
  final SafetyController ctrl;
  final List<Widget> extra; // other cards (tasks due, mirror offline)
  final VoidCallback onViewLive;
  const SafetyPage({super.key, required this.ctrl, required this.extra, required this.onViewLive});

  void _snack(BuildContext c, String m) => ScaffoldMessenger.of(c).showSnackBar(SnackBar(content: Text(m)));

  Future<void> _mode(BuildContext c, Future<String?> Function(bool) f, bool v, {bool warnContact = false}) async {
    final err = await f(v);
    if (!c.mounted) return;
    if (err != null) {
      _snack(c, err);
    } else if (v && warnContact && ctrl.primary.isEmpty) {
      _snack(c, 'Tip: add an emergency contact in Settings so calls can be made.');
    }
  }

  Widget _banner(String text, Color c, IconData icon) => GCard(
        border: c,
        child: Row(children: [
          Icon(icon, color: c),
          const SizedBox(width: 12),
          Expanded(child: Text(text, style: TextStyle(color: c, fontSize: 13))),
        ]),
      );

  Widget _escalation(BuildContext c, Escalation e) {
    final col = e.state == 'answered' ? kGreen : kRed;
    return GCard(
      border: col,
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Icon(Icons.call, color: col),
          const SizedBox(width: 8),
          const Text('Emergency calls', style: TextStyle(fontWeight: FontWeight.w800)),
        ]),
        const SizedBox(height: 6),
        Text(e.text),
        if (e.state == 'waiting')
          const Padding(
            padding: EdgeInsets.only(top: 4),
            child: Text('Calls again in 1 minute if the alert is still active.', style: TextStyle(color: kMuted, fontSize: 12)),
          ),
        if (e.state == 'waiting' || e.state == 'failed' || e.state == 'confirm')
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Wrap(spacing: 8, runSpacing: 8, children: [
              OutlinedButton(onPressed: ctrl.markAnswered, child: const Text('They answered')),
              if (e.state == 'confirm')
                FilledButton(onPressed: ctrl.callAgain, child: const Text('Call again')),
            ]),
          ),
      ]),
    );
  }

  Widget _alertCard(BuildContext c, SafetyAlert a) {
    final col = _priorityColor(a.priority);
    Widget? thumb;
    if (a.thumbnailB64 != null && a.thumbnailB64!.isNotEmpty) {
      try {
        thumb = ClipRRect(
          borderRadius: BorderRadius.circular(10),
          child: Image.memory(base64Decode(a.thumbnailB64!), fit: BoxFit.cover, width: double.infinity, gaplessPlayback: true),
        );
      } catch (_) {}
    }
    return GCard(
      border: col,
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Icon(_typeIcon(a.type), color: col, size: 28),
          const SizedBox(width: 12),
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(a.title, style: const TextStyle(fontWeight: FontWeight.w800)),
              Text(_when(a.time), style: const TextStyle(color: kMuted, fontSize: 12)),
            ]),
          ),
        ]),
        const SizedBox(height: 8),
        Text(a.message, style: const TextStyle(color: kMuted, fontSize: 13)),
        if (thumb != null) ...[const SizedBox(height: 10), thumb],
        const SizedBox(height: 10),
        Wrap(spacing: 8, runSpacing: 8, children: [
          FilledButton.icon(onPressed: onViewLive, icon: const Icon(Icons.videocam, size: 18), label: const Text('View live')),
          if (a.needsCall)
            OutlinedButton.icon(
              onPressed: ctrl.primary.isEmpty && ctrl.secondary.isEmpty ? null : () => ctrl.callNow(a),
              icon: const Icon(Icons.call, size: 18),
              label: const Text('Call now'),
            ),
          OutlinedButton(
            onPressed: () async {
              final err = await ctrl.dismiss(a);
              if (err != null && c.mounted) _snack(c, err);
            },
            child: const Text('Dismiss'),
          ),
        ]),
      ]),
    );
  }

  @override
  Widget build(BuildContext context) {
    final s = ctrl.status;
    final recent = ctrl.events
        .where((e) => e.type != 'ALL_CLEAR' && e.type != 'PET_SHADOW_FILTERED')
        .take(15)
        .toList();
    final nothing = ctrl.alerts.isEmpty && extra.isEmpty;

    return RefreshIndicator(
      onRefresh: ctrl.poll,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.all(14),
        children: [
          if (ctrl.checked && !ctrl.reachable && ctrl.diagnosis != null)
            _banner(
              ctrl.diagnosis!.message,
              ctrl.diagnosis!.isConfig ? kRed : kAmber,
              ctrl.diagnosis!.issue == ConnIssue.tailscaleOff ? Icons.vpn_key_off : (ctrl.diagnosis!.isConfig ? Icons.key_off : Icons.wifi_off),
            ),

          if (ctrl.escalation != null) _escalation(context, ctrl.escalation!),

          // --- modes ---
          GCard(
            child: Column(children: [
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                secondary: const Icon(Icons.shield_outlined),
                title: const Text('Security Mode'),
                subtitle: const Text('Alerts you if a person is seen. Use when nobody is home.'),
                value: s?.securityMode ?? false,
                onChanged: s == null ? null : (v) => _mode(context, ctrl.setSecurity, v),
              ),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                secondary: const Icon(Icons.elderly),
                title: const Text('Home Alone Mode'),
                subtitle: const Text('Watches for falls and no movement. Calls your contact if needed. Works only while this app is open.'),
                value: s?.homeAloneMode ?? false,
                onChanged: s == null ? null : (v) => _mode(context, ctrl.setHomeAlone, v, warnContact: true),
              ),
              if (s != null && !s.cameraEnabled)
                const Padding(
                  padding: EdgeInsets.only(top: 4),
                  child: Text('Camera is off, so detection is paused.', style: TextStyle(color: kAmber, fontSize: 12)),
                ),
            ]),
          ),

          // --- active alerts ---
          ...ctrl.alerts.map((a) => _alertCard(context, a)),
          ...extra,

          if (nothing)
            const GCard(
              child: Row(children: [
                Icon(Icons.check_circle, color: kGreen, size: 30),
                SizedBox(width: 12),
                Expanded(
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text('All clear', style: TextStyle(fontWeight: FontWeight.w700)),
                    Text('Nothing needs your attention right now.', style: TextStyle(color: kMuted, fontSize: 13)),
                  ]),
                ),
              ]),
            ),

          // --- history ---
          if (recent.isNotEmpty) ...[
            const Padding(
              padding: EdgeInsets.only(top: 8, bottom: 8),
              child: Text('Recent events', style: TextStyle(fontSize: 16, fontWeight: FontWeight.w800)),
            ),
            GCard(
              padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 4),
              child: Column(
                children: recent
                    .map((e) => ListTile(
                          dense: true,
                          leading: Icon(_typeIcon(e.type), color: _priorityColor(e.priority), size: 22),
                          title: Text(e.title),
                          subtitle: Text('${_when(e.time)}\n${e.message}', style: const TextStyle(fontSize: 12)),
                          isThreeLine: true,
                        ))
                    .toList(),
              ),
            ),
          ],
        ],
      ),
    );
  }
}

/// Extra rows for the Settings tab: contacts, stream quality, voice mute.
class SafetySettingsCards extends StatelessWidget {
  final SafetyController ctrl;
  const SafetySettingsCards({super.key, required this.ctrl});

  Future<void> _editContacts(BuildContext context) async {
    final a = TextEditingController(text: ctrl.primary);
    final b = TextEditingController(text: ctrl.secondary);
    bool auto = ctrl.autoCall;
    String? errA;
    String? errB;
    String cleanA = '';
    String cleanB = '';
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setD) => AlertDialog(
          title: const Text('Emergency contacts'),
          content: Column(mainAxisSize: MainAxisSize.min, children: [
            TextField(
              controller: a,
              keyboardType: TextInputType.phone,
              decoration: InputDecoration(labelText: 'Primary number', hintText: '+92 300 1234567', errorText: errA),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: b,
              keyboardType: TextInputType.phone,
              decoration: InputDecoration(labelText: 'Secondary number (optional)', errorText: errB),
            ),
            const SizedBox(height: 8),
            SwitchListTile(
              contentPadding: EdgeInsets.zero,
              title: const Text('Call automatically'),
              subtitle: const Text('Only while the app is open.'),
              value: auto,
              onChanged: (v) => setD(() => auto = v),
            ),
          ]),
          actions: [
            TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
            FilledButton(
              onPressed: () {
                final ca = SafetyController.cleanPhone(a.text);
                final cb = SafetyController.cleanPhone(b.text);
                if (ca == null || cb == null) {
                  setD(() {
                    errA = ca == null ? 'Enter digits only, like +923001234567' : null;
                    errB = cb == null ? 'Enter digits only, like +923001234567' : null;
                  });
                  return;
                }
                cleanA = ca;
                cleanB = cb;
                Navigator.pop(ctx, true);
              },
              child: const Text('Save'),
            ),
          ],
        ),
      ),
    );
    if (ok == true) await ctrl.saveContacts(cleanA, cleanB, auto);
  }

  @override
  Widget build(BuildContext context) {
    final s = ctrl.status;
    // Take the messenger now: the commands below finish after an await.
    final messenger = ScaffoldMessenger.of(context);
    void snack(String? err) {
      if (err != null) messenger.showSnackBar(SnackBar(content: Text(err)));
    }

    return Column(children: [
      GCard(
        child: Column(children: [
          ListTile(
            contentPadding: EdgeInsets.zero,
            leading: const Icon(Icons.contact_phone),
            title: const Text('Emergency contacts'),
            subtitle: Text(ctrl.primary.isEmpty
                ? 'Not set. Needed for fall alerts.'
                : '${ctrl.primary}${ctrl.secondary.isEmpty ? '' : '\n${ctrl.secondary}'}'),
            trailing: TextButton(onPressed: () => _editContacts(context), child: Text(ctrl.primary.isEmpty ? 'Add' : 'Edit')),
          ),
          if (ctrl.callLog.isNotEmpty) ...[
            const Divider(),
            Align(
              alignment: Alignment.centerLeft,
              child: Text(
                'Last call: ${ctrl.callLog.first.who.toLowerCase()}, ${ctrl.callLog.first.result} '
                '(${_when(DateTime.fromMillisecondsSinceEpoch(ctrl.callLog.first.time * 1000))})',
                style: const TextStyle(color: kMuted, fontSize: 12),
              ),
            ),
          ],
        ]),
      ),
      GCard(
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Text('Stream quality', style: TextStyle(fontWeight: FontWeight.w700)),
          const SizedBox(height: 4),
          const Text('Choose 480p if the video lags on a weak connection or mobile data.', style: TextStyle(color: kMuted, fontSize: 13)),
          const SizedBox(height: 10),
          SegmentedButton<String>(
            segments: const [
              ButtonSegment(value: '720p', label: Text('720p')),
              ButtonSegment(value: '480p', label: Text('480p')),
            ],
            selected: {s?.resolution ?? '720p'},
            onSelectionChanged: s == null ? null : (v) async => snack(await ctrl.setResolution(v.first)),
          ),
          const Divider(height: 24),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('Mute voice assistant'),
            subtitle: const Text('Safety prompts like "Are you okay?" still play.'),
            value: s?.voiceMuted ?? false,
            onChanged: s == null ? null : (v) async => snack(await ctrl.setVoiceMuted(v)),
          ),
        ]),
      ),
    ]);
  }
}
