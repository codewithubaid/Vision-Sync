import 'package:flutter/material.dart';
import 'config.dart';
import 'connection.dart';
import 'google_api.dart';
import 'home_page.dart';
import 'models.dart';
import 'safety_controller.dart';
import 'theme.dart';
import 'local_store.dart';

/// Remembers the paired mirror. Pair once, then go straight to the app.
class HomeGate extends StatefulWidget {
  const HomeGate({super.key});

  @override
  State<HomeGate> createState() => _HomeGateState();
}

class _HomeGateState extends State<HomeGate> {
  Mirror? _mirror;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    Mirror.loadSaved().then((m) async => m == null ? null : await applyHostOverride(m)).then((m) {
      if (mounted) {
        setState(() {
          _mirror = m;
          _loading = false;
        });
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) return const Scaffold(body: Center(child: CircularProgressIndicator()));
    final m = _mirror;
    if (m == null) {
      return MirrorPickerPage(onPicked: (picked) async {
        final withHost = await applyHostOverride(picked);
        await withHost.save();
        if (mounted) setState(() => _mirror = withHost);
      });
    }
    return HomePage(
      key: ValueKey(m.id),
      mirror: m,
      onSwitchMirror: () async {
        await Mirror.forget();
        if (mounted) setState(() => _mirror = null);
      },
    );
  }
}

class MirrorPickerPage extends StatefulWidget {
  final ValueChanged<Mirror> onPicked;
  const MirrorPickerPage({super.key, required this.onPicked});

  @override
  State<MirrorPickerPage> createState() => _MirrorPickerPageState();
}

class _MirrorPickerPageState extends State<MirrorPickerPage> {
  List<Mirror> _mirrors = [];
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      List rows;
      try {
        rows = await supabase.from('mirrors').select().order('created_at');
      } catch (_) {
        // The table may have no created_at column: load it without sorting.
        rows = await supabase.from('mirrors').select();
      }
      _mirrors = rows.map((r) => Mirror.fromMap(r as Map<String, dynamic>)).toList();
    } catch (e) {
      _error = 'Could not load mirrors: $e';
    }
    if (mounted) setState(() => _loading = false);
  }

  Future<void> _signOut() async {
    await GoogleApi.clear();
    await Mirror.forget();
    await SafetyController.clearLocalData();
    await LocalStore.clear();
    await supabase.auth.signOut();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Connect to your mirror'),
        actions: [
          IconButton(
            tooltip: 'Sign out',
            icon: const Icon(Icons.logout),
            onPressed: _signOut,
          ),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          if (_loading)
            const Padding(padding: EdgeInsets.all(32), child: Center(child: CircularProgressIndicator()))
          else if (_error != null)
            GCard(border: kAmber, child: Text(_error!, style: const TextStyle(color: kAmber)))
          else if (_mirrors.isEmpty)
            const GCard(child: Text('No mirrors found.', style: TextStyle(color: kMuted)))
          else
            ..._mirrors.map((m) => InkWell(
                  borderRadius: BorderRadius.circular(16),
                  onTap: () => widget.onPicked(m),
                  child: GCard(
                    child: Row(children: [
                      Expanded(
                        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                          Text(m.name, style: const TextStyle(fontWeight: FontWeight.w700)),
                          const SizedBox(height: 2),
                          Text('${m.address}:${m.port}${m.usingTailscale ? ' · Tailscale' : ''}${m.signal == null ? '' : ' · ${m.signal} dBm'}',
                              style: const TextStyle(color: kMuted, fontSize: 12)),
                        ]),
                      ),
                      const Text('Connect', style: TextStyle(color: kCyan, fontWeight: FontWeight.w600)),
                    ]),
                  ),
                )),
          const SizedBox(height: 8),
          OutlinedButton(onPressed: _load, child: const Text('Scan again')),
        ],
      ),
    );
  }
}
