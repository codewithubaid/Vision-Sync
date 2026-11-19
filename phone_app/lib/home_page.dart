import 'dart:async';
import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:supabase_flutter/supabase_flutter.dart';
import 'config.dart';
import 'direct_camera_page.dart';
import 'connection.dart';
import 'google_api.dart';
import 'local_store.dart';
import 'chat_page.dart';
import 'live_page.dart';
import 'mirror_api.dart';
import 'models.dart';
import 'push.dart';
import 'safety_controller.dart';
import 'safety_page.dart';
import 'theme.dart';

class HomePage extends StatefulWidget {
  final Mirror mirror;
  final VoidCallback onSwitchMirror;
  const HomePage({super.key, required this.mirror, required this.onSwitchMirror});

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> with WidgetsBindingObserver {
  int _tab = 0;
  int _calView = 0;
  late Mirror _mirror;
  late SafetyController _ctrl;
  bool _cam = true;
  Timer? _timer;
  StreamSubscription<AuthState>? _sub;

  bool _googleOn = false;
  bool _loadingG = false;
  String? _gError;
  List<GTask> _tasks = []; // from Google Tasks
  List<GTask> _localTasks = []; // typed in by hand, saved on this phone
  List<Reminder> _reminders = []; // typed in by hand, saved on this phone
  List<Reminder> _mirrorReminders = []; // kept on the mirror: said to Vision or added here
  List<GTask> _mirrorTasks = []; // kept on the mirror
  Map<String, String> _mirrorTaskGoogle = {}; // mirror task -> the same task in Google Tasks
  Set<String> _mirrorEventIds = {}; // Google Calendar events that are mirror reminders
  Set<String> _askedGoogle = {};
  bool _mirrorAgendaOk = false; // the mirror's list was read at least once
  bool _mirrorGoogle = false; // the mirror holds a Google sign-in
  String _mirrorGoogleEmail = '';
  String _mirrorGoogleProblem = '';
  bool _linking = false;
  Timer? _agendaTimer;
  List<CalEvent> _events = [];

  /// A mirror task that is also in Google Tasks (the mirror keeps them the same) is shown once.
  List<GTask> get _allTasks => [..._mirrorTasks.where((t) => !_alsoOnGoogle(t.id)), ..._localTasks, ..._tasks];
  bool _alsoOnGoogle(String id) {
    final g = _mirrorTaskGoogle[id] ?? '';
    return g.isNotEmpty && _tasks.any((t) => t.id == g);
  }
  List<Reminder> get _allReminders =>
      [..._mirrorReminders, ..._reminders]..sort((a, b) => a.when.compareTo(b.when));

  /// Offline only when the phone cannot reach the mirror AND its cloud "last seen" is old.
  /// (The mirror does not write last_seen yet, so an old value alone proves nothing.)
  bool get _offline => _ctrl.checked && !_ctrl.reachable && !_mirror.online;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _mirror = widget.mirror;
    _ctrl = SafetyController(_mirror)..addListener(_onCtrl);
    _ctrl.init();
    _refreshMirror(); // do not show the old saved "last seen" for the first 30 seconds
    _timer = Timer.periodic(const Duration(seconds: 30), (_) => _refreshMirror());
    // The mirror's tasks and reminders every 10 s while the app is open: what is said to Vision shows quickly.
    _agendaTimer = Timer.periodic(const Duration(seconds: 10), (_) {
      final state = WidgetsBinding.instance.lifecycleState;
      if (state == null || state == AppLifecycleState.resumed) unawaited(_loadMirrorAgenda());
    });
    _sub = supabase.auth.onAuthStateChange.listen((s) async {
      final t = s.session?.providerToken;
      if (t != null && !await GoogleApi.isDisabled()) {
        await GoogleApi.saveToken(t);
        _loadGoogle();
      }
      // Right after "Connect Google": the mirror gets the same account (its tasks and calendar).
      final r = s.session?.providerRefreshToken;
      if (r != null && r.isNotEmpty) {
        await GoogleApi.saveMirrorLink(r, t);
        unawaited(_linkMirrorGoogle());
      }
    });
    _init();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _timer?.cancel();
    _agendaTimer?.cancel();
    _sub?.cancel();
    _ctrl.removeListener(_onCtrl);
    _ctrl.dispose();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _ctrl.setForeground(state == AppLifecycleState.resumed);
    if (state == AppLifecycleState.resumed) {
      _ctrl.start();
      _refreshMirror();
      _loadGoogle();
    } else if (state == AppLifecycleState.paused) {
      _ctrl.stop();
    }
  }

  // The camera is the source of truth for the camera switch.
  void _onCtrl() {
    if (!mounted) return;
    final s = _ctrl.status;
    setState(() {
      if (s != null) _cam = s.cameraEnabled;
    });
  }

  Future<void> _init() async {
    final p = await SharedPreferences.getInstance();
    if (mounted) setState(() => _cam = p.getBool('cam') ?? true);
    final lt = await LocalStore.loadTasks();
    final lr = await LocalStore.loadReminders();
    if (mounted) {
      setState(() {
        _localTasks = lt;
        _reminders = lr;
      });
    }
    await _loadGoogle();
    await _loadMirrorAgenda();
  }

  void _snack(String m) {
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(m)));
  }

  DateTime _today() {
    final n = DateTime.now();
    return DateTime(n.year, n.month, n.day);
  }

  // ---------- data ----------
  Future<void> _refreshMirror() async {
    unawaited(_loadMirrorAgenda()); // the mirror's tasks and reminders
    try {
      final row = await supabase.from('mirrors').select().eq('id', _mirror.id).maybeSingle();
      if (row != null && mounted) {
        final m = await applyHostOverride(Mirror.fromMap(row)); // Tailscale address first
        await m.save();
        _ctrl.mirror = m;
        unawaited(Push.registerWith(MirrorApi(m))); // push notifications, see PUSH-SETUP.md
        setState(() => _mirror = m);
      }
    } catch (_) {
      // offline phone: keep the last known state
    }
  }

  bool _gBusy = false;

  Future<void> _loadGoogle() async {
    if (_gBusy) return; // resume, refresh and sign-in events can arrive together
    _gBusy = true;
    try {
      await _loadGoogleNow();
    } finally {
      _gBusy = false;
    }
  }

  Future<void> _loadGoogleNow() async {
    final tok = await GoogleApi.token() ?? await _tokenFromMirror();
    if (tok == null) {
      if (mounted) setState(() => _googleOn = false);
      // Signed in with Google but the 1-hour token ran out: refresh it. This opens the
      // Google page for a moment (Supabase does not refresh Google tokens itself).
      final viaGoogle = supabase.auth.currentUser?.appMetadata['provider'] == 'google';
      if (viaGoogle && !await GoogleApi.isDisabled() && await GoogleApi.shouldReauth()) {
        unawaited(connectGoogle(consent: false).catchError((_) => false));
      }
      return;
    }
    if (mounted) setState(() => _loadingG = true);
    try {
      final tasks = await GoogleApi.listTasks(tok);
      final events = await GoogleApi.listEvents(tok);
      if (mounted) {
        setState(() {
          _tasks = tasks;
          _events = events;
          _googleOn = true;
          _gError = null;
        });
      }
    } on GoogleAuthException {
      await GoogleApi.clear();
      if (mounted) {
        setState(() {
          _googleOn = false;
          _gError = 'Google permission missing or expired. Tap Connect.';
        });
      }
    } catch (_) {
      if (mounted) setState(() => _gError = 'Could not load Google data. Pull down to retry.');
    }
    if (mounted) setState(() => _loadingG = false);
  }

  Future<void> _toggle(GTask t) async {
    if (_onMirror(t.id)) {
      setState(() => t.done = !t.done);
      try {
        if (t.done) await _api.completeTask(_mirrorId(t.id));
      } catch (_) {
        _snack('Could not reach the mirror. Try again.');
      }
      await _loadMirrorAgenda();
      return;
    }
    if (isLocalTask(t)) {
      setState(() => t.done = !t.done);
      await LocalStore.saveTasks(_localTasks);
      return;
    }
    final tok = await GoogleApi.token();
    if (tok == null) {
      _snack('Google session expired. Tap Connect to continue.');
      _loadGoogle();
      return;
    }
    setState(() => t.done = !t.done);
    try {
      await GoogleApi.setDone(tok, t.id, t.done);
      _tellMirror();
    } catch (_) {
      if (mounted) setState(() => t.done = !t.done);
      _snack('Could not update the task.');
    }
  }

  Future<void> _delete(GTask t) async {
    if (_onMirror(t.id)) {
      setState(() => _mirrorTasks.remove(t));
      try {
        await _api.deleteTask(_mirrorId(t.id));
      } catch (_) {
        _snack('Could not reach the mirror. Try again.');
      }
      await _loadMirrorAgenda();
      return;
    }
    if (isLocalTask(t)) {
      setState(() => _localTasks.remove(t));
      await LocalStore.saveTasks(_localTasks);
      return;
    }
    final idx = _tasks.indexOf(t);
    setState(() {
      _tasks.remove(t);
      _mirrorTasks.removeWhere((m) => _mirrorTaskGoogle[m.id] == t.id); // its mirror copy goes too
    });
    final tok = await GoogleApi.token();
    if (tok == null) {
      // Nothing was deleted on Google: put the task back instead of hiding it.
      if (mounted) setState(() => _tasks.insert(idx < 0 ? 0 : idx.clamp(0, _tasks.length).toInt(), t));
      _snack('Google session expired. Tap Connect, then try again.');
      return;
    }
    try {
      await GoogleApi.deleteTask(tok, t.id);
      _tellMirror();
    } catch (_) {
      _snack('Could not delete the task.');
      _loadGoogle();
    }
  }

  Future<void> _addTask() async {
    final ctrl = TextEditingController();
    DateTime? due;
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setD) => AlertDialog(
          title: const Text('New task'),
          content: Column(mainAxisSize: MainAxisSize.min, children: [
            TextField(controller: ctrl, autofocus: true, decoration: const InputDecoration(hintText: 'Task name')),
            const SizedBox(height: 12),
            OutlinedButton.icon(
              onPressed: () async {
                final d = await showDatePicker(
                  context: ctx,
                  initialDate: DateTime.now(),
                  firstDate: DateTime.now().subtract(const Duration(days: 1)),
                  lastDate: DateTime.now().add(const Duration(days: 730)),
                );
                if (d != null) setD(() => due = d);
              },
              icon: const Icon(Icons.event),
              label: Text(due == null ? 'No due date' : dueLabel(due)),
            ),
          ]),
          actions: [
            TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
            FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Save')),
          ],
        ),
      ),
    );
    final title = ctrl.text.trim();
    if (ok != true || title.isEmpty) return;
    try {
      await _api.addTask(title, due: due); // on the mirror, and in Google Tasks when the mirror is synced
      await _loadMirrorAgenda();
      Future<void>.delayed(const Duration(seconds: 3), _loadMirrorAgenda); // then its Google copy shows
      _snack('Task saved on the mirror.');
      return;
    } catch (_) {
      // mirror not reachable: Google Tasks or this phone, as before
    }
    final tok = await GoogleApi.token();
    if (tok == null) {
      // Google is not connected: keep the task on this phone.
      final t = GTask(id: LocalStore.newId(), title: title, done: false, due: due);
      setState(() => _localTasks.insert(0, t));
      await LocalStore.saveTasks(_localTasks);
      _snack('Task saved on this phone.');
      return;
    }
    try {
      await GoogleApi.addTask(tok, title, due);
      _tellMirror();
      await _loadGoogle();
    } catch (_) {
      _snack('Could not save the task.');
    }
  }

  Future<void> _addReminder() async {
    final ctrl = TextEditingController();
    final soon = DateTime.now().add(const Duration(hours: 1));
    var date = DateTime(soon.year, soon.month, soon.day);
    var time = TimeOfDay(hour: soon.hour, minute: 0);
    String? err;
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setD) => AlertDialog(
          title: const Text('New reminder'),
          content: Column(mainAxisSize: MainAxisSize.min, children: [
            TextField(
              controller: ctrl,
              autofocus: true,
              decoration: InputDecoration(hintText: 'Reminder', errorText: err),
            ),
            const SizedBox(height: 12),
            SizedBox(
              width: double.infinity,
              child: OutlinedButton.icon(
                onPressed: () async {
                  final d = await showDatePicker(
                    context: ctx,
                    initialDate: date,
                    firstDate: DateTime.now().subtract(const Duration(days: 1)),
                    lastDate: DateTime.now().add(const Duration(days: 730)),
                  );
                  if (d != null) setD(() => date = d);
                },
                icon: const Icon(Icons.event),
                label: Text(dueLabel(date)),
              ),
            ),
            const SizedBox(height: 8),
            SizedBox(
              width: double.infinity,
              child: OutlinedButton.icon(
                onPressed: () async {
                  final t = await showTimePicker(context: ctx, initialTime: time);
                  if (t != null) setD(() => time = t);
                },
                icon: const Icon(Icons.schedule),
                label: Text(time.format(ctx)),
              ),
            ),
          ]),
          actions: [
            TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
            FilledButton(
              onPressed: () {
                if (ctrl.text.trim().isEmpty) {
                  setD(() => err = 'Type what to be reminded about.');
                } else {
                  Navigator.pop(ctx, true);
                }
              },
              child: const Text('Save'),
            ),
          ],
        ),
      ),
    );
    final title = ctrl.text.trim();
    if (ok != true || title.isEmpty || !mounted) return;
    final when = DateTime(date.year, date.month, date.day, time.hour, time.minute);
    try {
      await _api.addReminder(title, when); // kept on the mirror: shown there and said out loud
      await _loadMirrorAgenda();
      _snack('Reminder saved on the mirror. Vision will say it at ${timeLabel(when)}.');
      return;
    } catch (_) {
      // mirror not reachable: keep it on this phone
    }
    final r = Reminder(id: LocalStore.newId(), title: title, when: when);
    setState(() {
      _reminders.add(r);
      _reminders.sort((a, b) => a.when.compareTo(b.when));
    });
    await LocalStore.saveReminders(_reminders);
    _snack('Mirror not reachable: reminder saved on this phone.');
  }

  Future<void> _toggleReminder(Reminder r) async {
    if (_onMirror(r.id)) {
      // Done: Vision stops showing and saying it (an event from Google Calendar stays there).
      setState(() => _mirrorReminders.remove(r));
      try {
        await _api.doneReminder(_mirrorId(r.id));
      } catch (_) {
        try {
          await _api.deleteReminder(_mirrorId(r.id)); // an older mirror: take it off instead
        } catch (_) {
          _snack('Could not reach the mirror. Try again.');
        }
      }
      await _loadMirrorAgenda();
      return;
    }
    setState(() => r.done = !r.done);
    await LocalStore.saveReminders(_reminders);
  }

  Future<void> _deleteReminder(Reminder r) async {
    if (_onMirror(r.id)) {
      setState(() => _mirrorReminders.remove(r));
      try {
        await _api.deleteReminder(_mirrorId(r.id));
      } catch (_) {
        _snack('Could not reach the mirror. Try again.');
      }
      await _loadMirrorAgenda();
      return;
    }
    setState(() => _reminders.remove(r));
    await LocalStore.saveReminders(_reminders);
  }

  // ---------- tasks and reminders kept on the mirror ----------
  static const _mirrorPrefix = 'mirror:';
  bool _onMirror(String id) => id.startsWith(_mirrorPrefix);
  int _mirrorId(String id) => int.tryParse(id.substring(_mirrorPrefix.length)) ?? -1;
  MirrorApi get _api => MirrorApi(_mirror);

  /// Google Tasks changed from this phone: the mirror reads Google now, then the list is refreshed.
  void _tellMirror() {
    _api
        .syncGoogle()
        .then((_) => Future<void>.delayed(const Duration(seconds: 4)))
        .then((_) => _loadMirrorAgenda())
        .catchError((Object _) {});
  }

  /// Gives the mirror the Google account connected in this app, so the mirror syncs the same tasks
  /// and calendar. Tried again with every refresh until the mirror has it.
  Future<void> _linkMirrorGoogle() async {
    if (_linking) return;
    final link = await GoogleApi.pendingMirrorLink();
    if (link == null) return;
    _linking = true;
    try {
      final r = await _api.linkGoogle(link);
      await GoogleApi.clearMirrorLink();
      final email = (r['email'] ?? '').toString();
      final who = email.isEmpty ? 'your Google account' : email;
      final problem = (r['problem'] ?? '').toString();
      if (r['success'] == true) {
        _snack(problem.isEmpty ? 'The mirror now syncs $who.' : 'The mirror is linked to $who, but: $problem');
      } else {
        _snack('Mirror: ${problem.isEmpty ? 'it could not use this Google sign-in' : problem}');
      }
      unawaited(_loadMirrorAgenda());
    } catch (_) {
      // the mirror is not reachable now: tried again with the next refresh
    } finally {
      _linking = false;
    }
  }

  /// The mirror keeps the Google sign-in, so the app gets a fresh token from it instead of
  /// opening the Google page every hour. Only for this person's own account.
  Future<String?> _tokenFromMirror() async {
    if (await GoogleApi.isDisabled()) return null;
    try {
      final r = await _api.googleToken();
      final token = (r['access_token'] ?? '').toString();
      final email = (r['email'] ?? '').toString().toLowerCase();
      final me = (supabase.auth.currentUser?.email ?? '').toLowerCase();
      if (token.isEmpty || email.isEmpty || email != me) return null; // only this person's own account
      await GoogleApi.saveToken(token);
      return GoogleApi.token();
    } catch (_) {
      return null;
    }
  }

  /// "Disconnect" also stops the mirror syncing this account (never someone else's).
  void _unlinkMirrorIfMine() {
    final me = (supabase.auth.currentUser?.email ?? '').toLowerCase();
    final linked = _mirrorGoogleEmail.toLowerCase();
    if (linked.isNotEmpty && me.isNotEmpty && linked != me) return;
    _api.unlinkGoogle().then((_) => _loadMirrorAgenda()).catchError((Object _) {});
  }

  /// One line under the tabs: which Google account the mirror keeps in sync, or what is missing.
  List<Widget> _mirrorGoogleLine() {
    if (!_mirrorAgendaOk) return const [];
    final me = (supabase.auth.currentUser?.email ?? '').toLowerCase();
    final email = _mirrorGoogleEmail;
    final mine = email.isEmpty || me.isEmpty || email.toLowerCase() == me;
    final ok = _mirrorGoogle && _mirrorGoogleProblem.isEmpty;
    final String text;
    if (ok) {
      text = 'Mirror synced with ${email.isEmpty ? 'your Google account' : email}';
    } else if (_mirrorGoogleProblem.isNotEmpty) {
      text = 'Mirror: $_mirrorGoogleProblem';
    } else {
      text = 'Mirror: not synced with Google yet.';
    }
    return [
      Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: Row(children: [
          Icon(ok ? Icons.cloud_done_outlined : Icons.cloud_off_outlined, size: 18, color: ok ? kGreen : kAmber),
          const SizedBox(width: 8),
          Expanded(child: Text(text, style: const TextStyle(color: kMuted, fontSize: 13))),
          if (_googleOn && (!ok || !mine))
            TextButton(onPressed: () => connectGoogle(), child: Text(mine ? 'Sync mirror' : 'Use my account')),
        ]),
      ),
    ];
  }

  /// Reads the mirror's own list (what was said to Vision): on start, every 30 s and after a change.
  Future<void> _loadMirrorAgenda() async {
    try {
      final j = await _api.agenda();
      final rems = <Reminder>[];
      final eventIds = <String>{};
      final taskGoogle = <String, String>{};
      for (final item in (j['reminders'] as List? ?? const [])) {
        final m = item as Map<String, dynamic>;
        final when = DateTime.tryParse((m['due_at'] ?? '').toString());
        if (when == null) continue;
        rems.add(Reminder(id: '$_mirrorPrefix${m['id']}', title: (m['title'] ?? '').toString(), when: when));
        final event = (m['google_id'] ?? '').toString();
        if (event.isNotEmpty) eventIds.add(event);
      }
      final tasks = <GTask>[];
      for (final item in (j['tasks'] as List? ?? const [])) {
        final m = item as Map<String, dynamic>;
        tasks.add(GTask(
          id: '$_mirrorPrefix${m['id']}',
          title: (m['title'] ?? '').toString(),
          done: m['done'] == true,
          due: DateTime.tryParse((m['date'] ?? '').toString()),
        ));
        taskGoogle['$_mirrorPrefix${m['id']}'] = (m['google_id'] ?? '').toString();
      }
      if (mounted) {
        setState(() {
          _mirrorReminders = rems;
          _mirrorTasks = tasks;
          _mirrorTaskGoogle = taskGoogle;
          _mirrorEventIds = eventIds;
          _mirrorAgendaOk = true;
          _mirrorGoogle = j['google'] == true;
          _mirrorGoogleEmail = (j['google_email'] ?? '').toString();
          _mirrorGoogleProblem = (j['google_problem'] ?? '').toString();
        });
        // The mirror just put a task on Google Tasks: load Google again so it shows now.
        final unseen = taskGoogle.values.where((g) => g.isNotEmpty && !_tasks.any((t) => t.id == g)).toSet();
        if (_googleOn && unseen.isNotEmpty && !_askedGoogle.containsAll(unseen)) {
          _askedGoogle = {..._askedGoogle, ...unseen};
          _loadGoogle();
        }
        unawaited(_linkMirrorGoogle()); // a Google sign-in still to give to the mirror
      }
    } catch (_) {
      // mirror not reachable: keep the last list
    }
  }

  /// Lets the user type the mirror's Tailscale IP (100.x.x.x) or MagicDNS name.
  Future<void> _editHost() async {
    final c = TextEditingController(text: _mirror.usingTailscale ? _mirror.address : '');
    String? err;
    String clean = '';
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setD) => AlertDialog(
          title: const Text('Mirror Tailscale address'),
          content: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
            TextField(
              controller: c,
              autofocus: true,
              keyboardType: TextInputType.url,
              decoration: InputDecoration(hintText: '100.101.102.103 or mirror.tailnet.ts.net', errorText: err),
            ),
            const SizedBox(height: 8),
            const Text('Find it in the Tailscale app on the mirror. Leave empty to remove it.',
                style: TextStyle(color: kMuted, fontSize: 12)),
          ]),
          actions: [
            TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
            FilledButton(
              onPressed: () {
                final v = validateHost(c.text);
                if (v == null) {
                  setD(() => err = 'Enter an IP like 100.101.102.103 or a name like mirror.tailnet.ts.net (no port).');
                } else {
                  clean = v;
                  Navigator.pop(ctx, true);
                }
              },
              child: const Text('Save'),
            ),
          ],
        ),
      ),
    );
    if (ok != true) return;
    await saveHostOverride(clean);
    if (clean.isNotEmpty) {
      final m = _mirror.withHost(clean);
      await m.save();
      _ctrl.mirror = m;
      if (mounted) setState(() => _mirror = m);
    } else {
      // Drop the old address right away (works offline too), then take whatever
      // Supabase / the build default says.
      final m = await applyHostOverride(_mirror.withHost(null));
      await m.save();
      _ctrl.mirror = m;
      if (mounted) setState(() => _mirror = m);
      await _refreshMirror();
    }
    _ctrl.poll();
  }

  Future<void> _toggleCam() async {
    final next = !_cam;
    final err = await _ctrl.setCamera(next); // sends the real command to the mirror
    if (err != null) {
      _snack(err);
      return;
    }
    final p = await SharedPreferences.getInstance();
    await p.setBool('cam', next);
    if (mounted) setState(() => _cam = next);
    _snack(next ? 'Camera is on' : 'Camera is off. Security and fall detection are paused.');
  }

  String? _liveProblem() {
    if (_ctrl.checked && !_ctrl.reachable) return _ctrl.diagnosis?.message ?? 'Mirror not reachable.';
    return null;
  }

  Future<void> _signOut() async {
    _ctrl.stop(); // a poll must not write handled alerts back after they are cleared
    await GoogleApi.clear();
    await Mirror.forget();
    await SafetyController.clearLocalData(); // contacts and call history belong to this account
    await LocalStore.clear(); // so do the tasks and reminders typed in by hand
    await supabase.auth.signOut();
  }

  // ---------- UI ----------
  @override
  Widget build(BuildContext context) {
    final Widget body;
    switch (_tab) {
      case 0:
        body = _alerts();
        break;
      case 1:
        body = LivePage(mirror: _mirror, camOn: _cam, problem: _liveProblem());
        break;
      case 2:
        body = _tasksTab();
        break;
      case 3:
        body = _status();
        break;
      default:
        body = _settings();
    }

    return Scaffold(
      appBar: AppBar(
        title: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Text('Glass Connect', style: TextStyle(color: kCyan, fontWeight: FontWeight.w800, fontSize: 16)),
          Text(_mirror.name, style: const TextStyle(fontSize: 12, color: kMuted)),
        ]),
        actions: [
          IconButton(
            tooltip: 'Assistant',
            icon: const Icon(Icons.chat_bubble_outline),
            onPressed: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => ChatPage(mirror: _mirror))),
          ),
          Padding(
            padding: const EdgeInsets.only(right: 10),
            child: ActionChip(
              avatar: Icon(Icons.videocam, size: 16, color: _cam ? Colors.white : kMuted),
              label: Text(_cam ? 'CAM ON' : 'CAM OFF',
                  style: TextStyle(color: _cam ? Colors.white : kMuted, fontWeight: FontWeight.w700, fontSize: 12)),
              backgroundColor: _cam ? kRed : kCard,
              onPressed: _toggleCam,
            ),
          ),
        ],
      ),
      body: Column(children: [
        if (_offline)
          Container(
            width: double.infinity,
            margin: const EdgeInsets.fromLTRB(14, 0, 14, 8),
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: const Color(0x26F59E0B),
              border: Border.all(color: kAmber),
              borderRadius: BorderRadius.circular(12),
            ),
            child: const Text('Mirror offline. Showing the last known state. It reconnects on its own.',
                style: TextStyle(color: kAmber, fontSize: 13)),
          ),
        Expanded(child: body),
      ]),
      floatingActionButton: _tab == 2
          ? FloatingActionButton(
              tooltip: _calView == 0 ? 'Add task' : 'Add reminder',
              onPressed: _calView == 0 ? _addTask : _addReminder,
              child: const Icon(Icons.add),
            )
          : null,
      bottomNavigationBar: NavigationBar(
        selectedIndex: _tab,
        onDestinationSelected: (i) => setState(() => _tab = i),
        destinations: [
          NavigationDestination(
              icon: Badge(
                  isLabelVisible: _ctrl.alerts.isNotEmpty,
                  label: Text('${_ctrl.alerts.length}'),
                  child: const Icon(Icons.notifications_outlined)),
              selectedIcon: const Icon(Icons.notifications),
              label: 'Alerts'),
          const NavigationDestination(icon: Icon(Icons.videocam_outlined), selectedIcon: Icon(Icons.videocam), label: 'Live'),
          const NavigationDestination(icon: Icon(Icons.check_circle_outline), selectedIcon: Icon(Icons.check_circle), label: 'Tasks'),
          const NavigationDestination(icon: Icon(Icons.monitor_heart_outlined), selectedIcon: Icon(Icons.monitor_heart), label: 'Status'),
          const NavigationDestination(icon: Icon(Icons.settings_outlined), selectedIcon: Icon(Icons.settings), label: 'Settings'),
        ],
      ),
    );
  }

  // ----- Alerts -----
  Widget _alertCard(IconData icon, String title, String sub, Color c, {VoidCallback? onTap}) {
    return InkWell(
      borderRadius: BorderRadius.circular(16),
      onTap: onTap,
      child: GCard(
        border: c,
        child: Row(children: [
          Icon(icon, color: c, size: 28),
          const SizedBox(width: 12),
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(title, style: const TextStyle(fontWeight: FontWeight.w700)),
              const SizedBox(height: 2),
              Text(sub, style: const TextStyle(color: kMuted, fontSize: 13)),
            ]),
          ),
        ]),
      ),
    );
  }

  Widget _alerts() {
    final cards = <Widget>[];
    if (_offline) {
      cards.add(_alertCard(Icons.wifi_off, 'Mirror is offline', 'Showing the last known state. It reconnects on its own.', kRed));
    }
    final due = _allTasks.where((t) => !t.done && t.due != null && !t.due!.isAfter(_today())).toList();
    if (due.isNotEmpty) {
      final plural = due.length > 1 ? 's' : '';
      final more = due.length > 1 ? ' and more' : '';
      cards.add(_alertCard(Icons.check_circle_outline, '${due.length} task$plural due or overdue', '${due.first.title}$more', kCyan,
          onTap: () => setState(() => _tab = 2)));
    }
    final dueR = _allReminders.where((r) => !r.done && DateTime(r.when.year, r.when.month, r.when.day).compareTo(_today()) <= 0).toList();
    if (dueR.isNotEmpty) {
      final plural = dueR.length > 1 ? 's' : '';
      final more = dueR.length > 1 ? ' and more' : '';
      cards.add(_alertCard(Icons.notifications_active_outlined, '${dueR.length} reminder$plural today or overdue',
          '${dueR.first.title}$more', kAmber, onTap: () => setState(() {
                _tab = 2;
                _calView = 2;
              })));
    }
    return SafetyPage(ctrl: _ctrl, extra: cards, onViewLive: () => setState(() => _tab = 1));
  }

  // ----- Tasks & calendar -----
  Widget _connectCard() {
    return GCard(
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        const Text('Connect Google', style: TextStyle(fontWeight: FontWeight.w700)),
        const SizedBox(height: 4),
        Text(_gError ?? 'See your Google Tasks and Calendar here. You can still add tasks and reminders with +.',
            style: const TextStyle(color: kMuted)),
        const SizedBox(height: 12),
        FilledButton(onPressed: () => connectGoogle(), child: const Text('Connect Google')),
      ]),
    );
  }

  Widget _tasksTab() {
    return RefreshIndicator(
      onRefresh: _loadGoogle,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(14, 14, 14, 90), // room so the + button never covers the last item
        children: [
          SegmentedButton<int>(
            segments: const [
              ButtonSegment(value: 0, label: Text('Tasks')),
              ButtonSegment(value: 1, label: Text('Calendar')),
              ButtonSegment(value: 2, label: Text('Reminders')),
            ],
            selected: {_calView},
            onSelectionChanged: (s) => setState(() => _calView = s.first),
          ),
          const SizedBox(height: 12),
          ..._mirrorGoogleLine(),
          // Tasks and reminders you type in work without Google, so this is only a hint.
          if (!_googleOn && _calView != 2) _connectCard(),
          if (_googleOn && _loadingG && _tasks.isEmpty && _events.isEmpty)
            const Padding(padding: EdgeInsets.all(24), child: Center(child: CircularProgressIndicator()))
          else if (_calView == 0)
            ..._taskList()
          else if (_calView == 1)
            ..._eventList()
          else
            ..._reminderList(),
        ],
      ),
    );
  }

  List<Widget> _taskList() {
    final all = _allTasks;
    if (all.isEmpty) {
      return [const GCard(child: Text('No tasks yet. Tap + to add one.', style: TextStyle(color: kMuted)))];
    }
    final open = all.where((t) => !t.done);
    final done = all.where((t) => t.done);
    return [...open, ...done].map(_taskTile).toList();
  }

  Widget _taskTile(GTask t) {
    final overdue = !t.done && t.due != null && t.due!.isBefore(_today());
    return Dismissible(
      key: ValueKey(t.id),
      direction: DismissDirection.endToStart,
      onDismissed: (_) => _delete(t),
      background: Container(
        margin: const EdgeInsets.only(bottom: 12),
        padding: const EdgeInsets.only(right: 20),
        alignment: Alignment.centerRight,
        decoration: BoxDecoration(color: kRed, borderRadius: BorderRadius.circular(16)),
        child: const Icon(Icons.delete, color: Colors.white),
      ),
      child: GCard(
        padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
        child: ListTile(
          contentPadding: const EdgeInsets.symmetric(horizontal: 8),
          leading: Checkbox(value: t.done, onChanged: (_) => _toggle(t)),
          title: Text(t.title,
              style: TextStyle(
                  decoration: t.done ? TextDecoration.lineThrough : null, color: t.done ? kMuted : null)),
          subtitle: Text(dueLabel(t.due), style: TextStyle(color: overdue ? kAmber : kMuted)),
        ),
      ),
    );
  }

  /// Google events plus the reminders typed in by hand, for today and the next 2 days.
  List<Widget> _eventList() {
    final from = _today();
    final to = DateTime(from.year, from.month, from.day + 3);
    final items = <({DateTime start, Widget card})>[];
    for (final e in _events) {
      if (_mirrorEventIds.contains(e.id)) continue; // a mirror reminder: shown once, as the reminder
      items.add((
        start: e.start,
        card: GCard(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(e.title, style: const TextStyle(fontWeight: FontWeight.w700)),
            const SizedBox(height: 2),
            Text(e.location.isEmpty ? e.timeText : '${e.timeText} · ${e.location}',
                style: const TextStyle(color: kMuted, fontSize: 13)),
          ]),
        ),
      ));
    }
    for (final r in _allReminders) {
      if (r.when.isBefore(from) || !r.when.isBefore(to)) continue;
      items.add((
        start: r.when,
        card: GCard(
          border: kAmber,
          child: Row(children: [
            const Icon(Icons.notifications_active_outlined, color: kAmber, size: 20),
            const SizedBox(width: 10),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(r.title,
                    style: TextStyle(
                        fontWeight: FontWeight.w700,
                        decoration: r.done ? TextDecoration.lineThrough : null,
                        color: r.done ? kMuted : null)),
                const SizedBox(height: 2),
                Text('Reminder · ${timeLabel(r.when)}', style: const TextStyle(color: kMuted, fontSize: 13)),
              ]),
            ),
          ]),
        ),
      ));
    }
    if (items.isEmpty) {
      return [const GCard(child: Text('Nothing in the next 3 days. Tap + to add a reminder.', style: TextStyle(color: kMuted)))];
    }
    items.sort((a, b) => a.start.compareTo(b.start));
    final out = <Widget>[];
    String? last;
    for (final it in items) {
      final label = dayLabel(it.start);
      if (label != last) {
        out.add(Padding(
          padding: const EdgeInsets.only(top: 4, bottom: 8),
          child: Text(label, style: const TextStyle(color: kMuted, fontWeight: FontWeight.w700)),
        ));
        last = label;
      }
      out.add(it.card);
    }
    return out;
  }

  List<Widget> _reminderList() {
    if (_allReminders.isEmpty) {
      return [const GCard(child: Text('No reminders yet. Tap + to add one.', style: TextStyle(color: kMuted)))];
    }
    final open = _allReminders.where((r) => !r.done);
    final done = _allReminders.where((r) => r.done);
    return [...open, ...done].map(_reminderTile).toList();
  }

  Widget _reminderTile(Reminder r) {
    final overdue = !r.done && r.when.isBefore(DateTime.now());
    return Dismissible(
      key: ValueKey(r.id),
      direction: DismissDirection.endToStart,
      onDismissed: (_) => _deleteReminder(r),
      background: Container(
        margin: const EdgeInsets.only(bottom: 12),
        padding: const EdgeInsets.only(right: 20),
        alignment: Alignment.centerRight,
        decoration: BoxDecoration(color: kRed, borderRadius: BorderRadius.circular(16)),
        child: const Icon(Icons.delete, color: Colors.white),
      ),
      child: GCard(
        padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
        child: ListTile(
          contentPadding: const EdgeInsets.symmetric(horizontal: 8),
          leading: Checkbox(value: r.done, onChanged: (_) => _toggleReminder(r)),
          title: Text(r.title,
              style: TextStyle(decoration: r.done ? TextDecoration.lineThrough : null, color: r.done ? kMuted : null)),
          subtitle: Text('${dayLabel(r.when)} · ${timeLabel(r.when)}${_onMirror(r.id) ? ' · on the mirror' : ''}',
              style: TextStyle(color: overdue ? kAmber : kMuted)),
        ),
      ),
    );
  }

  // ----- Status -----
  Widget _kv(String k, String v) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 4),
        child: Row(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
          Text(k, style: const TextStyle(color: kMuted)),
          Flexible(child: Text(v, textAlign: TextAlign.right)),
        ]),
      );

  Widget _status() {
    final problems = <String>[];
    if (_offline) problems.add('Mirror is offline');
    if (_mirror.signal != null && _mirror.signal! <= -75) problems.add('Wi-Fi signal is weak (${_mirror.signal} dBm)');
    final last = _mirror.lastSeen;
    final live = _ctrl.status;
    if (_ctrl.checked && !_ctrl.reachable && _ctrl.diagnosis != null) problems.add(_ctrl.diagnosis!.title);
    final temp = live?.cpuTempC;
    if (temp != null && temp >= 85) problems.add('Mirror CPU is very hot ($temp °C)');
    final wifi = live?.wifiDbm ?? _mirror.signal;
    return ListView(padding: const EdgeInsets.all(14), children: [
      if (problems.isEmpty)
        const GCard(
          child: Row(children: [
            Icon(Icons.check_circle, color: kGreen, size: 30),
            SizedBox(width: 12),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text('Mirror is healthy', style: TextStyle(fontWeight: FontWeight.w700)),
                Text('Everything is running normally.', style: TextStyle(color: kMuted, fontSize: 13)),
              ]),
            ),
          ]),
        )
      else
        GCard(
          border: kAmber,
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text('${problems.length} issue${problems.length > 1 ? 's' : ''} found',
                style: const TextStyle(fontWeight: FontWeight.w700)),
            ...problems.map((p) => Padding(padding: const EdgeInsets.only(top: 6), child: Text('• $p'))),
          ]),
        ),
      GCard(
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Text('Live from the mirror', style: TextStyle(fontWeight: FontWeight.w700)),
          const SizedBox(height: 6),
          if (live == null)
            const Text('No data yet. The mirror is not reachable.', style: TextStyle(color: kMuted))
          else ...[
            _kv('CPU temperature', live.cpuTempC == null ? 'Unknown' : '${live.cpuTempC} °C'),
            _kv('AI mode', live.aiMode == 'cloud' ? 'Cloud (online)' : 'Local (on the mirror)'),
            _kv('Wi-Fi signal', wifi == null ? 'Unknown' : '$wifi dBm'),
            _kv('Camera', live.cameraEnabled ? 'On' : 'Off'),
            _kv('Security Mode', live.securityMode ? 'ON' : 'Off'),
            _kv('Home Alone Mode', live.homeAloneMode ? 'ON' : 'Off'),
            _kv('Stream quality', live.resolution),
          ],
        ]),
      ),
      GCard(
        padding: EdgeInsets.zero,
        child: Theme(
          data: Theme.of(context).copyWith(dividerColor: Colors.transparent),
          child: ExpansionTile(
            title: const Text('Advanced'),
            childrenPadding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
            children: [
              _kv('Name', _mirror.name),
              _kv('Address', '${_mirror.address}:${_mirror.port}${_mirror.usingTailscale ? ' (Tailscale)' : ' (home network)'}'),
              _kv('Wi-Fi signal', _mirror.signal == null ? 'Unknown' : '${_mirror.signal} dBm'),
              _kv('Last seen', last == null ? 'No heartbeat yet' : '${dayLabel(last.toLocal())} ${timeLabel(last.toLocal())}'),
              _kv('Camera', _cam ? 'On' : 'Off'),
            ],
          ),
        ),
      ),
    ]);
  }

  // ----- Settings -----
  Widget _settings() {
    final u = supabase.auth.currentUser;
    final meta = u?.userMetadata ?? <String, dynamic>{};
    final name = (meta['full_name'] ?? meta['name'] ?? u?.email ?? 'Account').toString();
    final initial = name.trim().isEmpty ? '?' : name.trim()[0].toUpperCase();
    return ListView(padding: const EdgeInsets.all(14), children: [
      GCard(
        child: Row(children: [
          CircleAvatar(backgroundColor: kCyan, foregroundColor: const Color(0xFF04141A), child: Text(initial)),
          const SizedBox(width: 12),
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(name, style: const TextStyle(fontWeight: FontWeight.w700), overflow: TextOverflow.ellipsis),
              Text(u?.email ?? '', style: const TextStyle(color: kMuted, fontSize: 13), overflow: TextOverflow.ellipsis),
            ]),
          ),
        ]),
      ),
      GCard(
        child: Column(children: [
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('Camera'),
            subtitle: Text(_cam ? 'On. Tap to turn off.' : 'Off'),
            value: _cam,
            onChanged: (_) => _toggleCam(),
          ),
          ListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('Google Calendar & Tasks'),
            subtitle: Text(_googleOn ? 'Connected' : 'Not connected'),
            trailing: TextButton(
              onPressed: () async {
                if (_googleOn) {
                  await GoogleApi.clear(disable: true);
                  await GoogleApi.clearMirrorLink();
                  _unlinkMirrorIfMine();
                  if (mounted) {
                    setState(() {
                      _googleOn = false;
                      _tasks = [];
                      _events = [];
                    });
                  }
                } else {
                  connectGoogle();
                }
              },
              child: Text(_googleOn ? 'Disconnect' : 'Connect'),
            ),
          ),
        ]),
      ),
      GCard(
        child: ListTile(
          contentPadding: EdgeInsets.zero,
          leading: const Icon(Icons.vpn_lock),
          title: const Text('Mirror address (Tailscale)'),
          subtitle: Text(_mirror.usingTailscale
              ? '${_mirror.address}:${_mirror.port}'
              : 'Not set. Using ${_mirror.ip}, which only works on the mirror\'s Wi-Fi.'),
          trailing: TextButton(onPressed: _editHost, child: Text(_mirror.usingTailscale ? 'Edit' : 'Set')),
        ),
      ),
      GCard(
        child: ListTile(
          contentPadding: EdgeInsets.zero,
          leading: const Icon(Icons.videocam_outlined),
          title: const Text('Direct camera stream'),
          subtitle: const Text('Watch an MJPEG camera server by its own address (VisionSync)'),
          trailing: const Icon(Icons.chevron_right),
          onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const LiveFeedScreen())),
        ),
      ),
      SafetySettingsCards(ctrl: _ctrl),
      OutlinedButton(onPressed: widget.onSwitchMirror, child: const Text('Switch mirror')),
      const SizedBox(height: 8),
      FilledButton(
        style: FilledButton.styleFrom(backgroundColor: kRed, foregroundColor: Colors.white),
        onPressed: _signOut,
        child: const Text('Sign out'),
      ),
    ]);
  }
}
