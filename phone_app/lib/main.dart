import 'dart:async';
import 'package:flutter/material.dart';
import 'package:supabase_flutter/supabase_flutter.dart';
import 'auth_page.dart';
import 'config.dart';
import 'google_api.dart';
import 'home_gate.dart';
import 'push.dart';
import 'theme.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await Supabase.initialize(url: kSupabaseUrl, anonKey: kSupabaseKey);
  await Push.init(); // does nothing until push is switched on, see PUSH-SETUP.md
  // Keep the Google token that arrives after a Google sign-in.
  supabase.auth.onAuthStateChange.listen((s) async {
    final t = s.session?.providerToken;
    if (t != null && !await GoogleApi.isDisabled()) await GoogleApi.saveToken(t);
    // Google gives its long-lived sign-in only right after "Connect Google": kept until the mirror
    // has it, so the mirror syncs the same account (home_page.dart hands it over).
    final r = s.session?.providerRefreshToken;
    if (r != null && r.isNotEmpty) await GoogleApi.saveMirrorLink(r, t);
  });
  runApp(const GlassApp());
}

class GlassApp extends StatelessWidget {
  const GlassApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Glass Connect',
      debugShowCheckedModeBanner: false,
      theme: glassTheme(),
      home: const AuthGate(),
    );
  }
}

/// Shows the sign-in page or the app, depending on the login state.
class AuthGate extends StatefulWidget {
  const AuthGate({super.key});

  @override
  State<AuthGate> createState() => _AuthGateState();
}

class _AuthGateState extends State<AuthGate> {
  StreamSubscription<AuthState>? _sub;

  @override
  void initState() {
    super.initState();
    _sub = supabase.auth.onAuthStateChange.listen((s) {
      if (s.event == AuthChangeEvent.passwordRecovery && mounted) _newPassword();
    });
  }

  @override
  void dispose() {
    _sub?.cancel();
    super.dispose();
  }

  void _snack(String m) => ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(m)));

  Future<void> _newPassword() async {
    final c = TextEditingController();
    String? err;
    // The dialog stays open until the password is valid (or the person cancels).
    final pw = await showDialog<String>(
      context: context,
      barrierDismissible: false,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setD) => AlertDialog(
          title: const Text('Choose a new password'),
          content: TextField(
            controller: c,
            obscureText: true,
            decoration: InputDecoration(hintText: '8+ characters, letters and numbers', errorText: err),
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Cancel')),
            FilledButton(
              onPressed: () {
                final v = c.text;
                if (v.length < 8 || !RegExp(r'[A-Za-z]').hasMatch(v) || !RegExp(r'\d').hasMatch(v)) {
                  setD(() => err = 'Use 8+ characters with letters and numbers.');
                } else {
                  Navigator.pop(ctx, v);
                }
              },
              child: const Text('Update'),
            ),
          ],
        ),
      ),
    );
    if (pw == null || !mounted) return;
    try {
      await supabase.auth.updateUser(UserAttributes(password: pw));
      if (mounted) _snack('Password updated');
    } on AuthException catch (e) {
      if (mounted) _snack(e.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    return StreamBuilder<AuthState>(
      stream: supabase.auth.onAuthStateChange,
      builder: (context, snap) {
        final session = snap.data?.session ?? supabase.auth.currentSession;
        return session == null ? const AuthPage() : const HomeGate();
      },
    );
  }
}
