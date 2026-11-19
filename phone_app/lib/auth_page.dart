import 'package:flutter/material.dart';
import 'package:supabase_flutter/supabase_flutter.dart';
import 'config.dart';
import 'google_api.dart';
import 'theme.dart';

enum _Mode { signIn, signUp, forgot }

class AuthPage extends StatefulWidget {
  const AuthPage({super.key});

  @override
  State<AuthPage> createState() => _AuthPageState();
}

class _AuthPageState extends State<AuthPage> {
  _Mode _mode = _Mode.signIn;
  final _name = TextEditingController();
  final _email = TextEditingController();
  final _pw = TextEditingController();
  final _pw2 = TextEditingController();
  bool _busy = false;
  bool _show = false;
  String? _error;
  String? _info;

  @override
  void dispose() {
    _name.dispose();
    _email.dispose();
    _pw.dispose();
    _pw2.dispose();
    super.dispose();
  }

  void _setMode(_Mode m) => setState(() {
        _mode = m;
        _error = null;
        _info = null;
      });

  void _fail(String m) {
    if (!mounted) return;
    setState(() {
      _error = m;
      _busy = false;
    });
  }

  String _friendly(String m) {
    final s = m.toLowerCase();
    if (s.contains('invalid login')) return 'Incorrect email or password.';
    if (s.contains('not confirmed')) return 'Please confirm your email first. Check your inbox.';
    if (s.contains('already registered') || s.contains('already exists')) {
      return 'An account with this email already exists. Try signing in.';
    }
    if (s.contains('rate limit') || s.contains('too many') || s.contains('seconds')) {
      return 'Too many attempts. Please wait a minute and try again.';
    }
    return m;
  }

  bool _pwOk(String p) => p.length >= 8 && RegExp(r'[A-Za-z]').hasMatch(p) && RegExp(r'\d').hasMatch(p);

  Future<void> _submit() async {
    if (_busy) return;
    final email = _email.text.trim();
    final pw = _pw.text;
    setState(() {
      _error = null;
      _info = null;
    });
    if (!RegExp(r'^[^\s@]+@[^\s@]+\.[^\s@]{2,}$').hasMatch(email)) return _fail('Enter a valid email address.');
    if (_mode == _Mode.signUp) {
      if (_name.text.trim().length < 2) return _fail('Enter your full name.');
      if (!_pwOk(pw)) return _fail('Use 8+ characters with letters and numbers.');
      if (pw != _pw2.text) return _fail('The two passwords do not match.');
    } else if (_mode == _Mode.signIn && pw.isEmpty) {
      return _fail('Enter your password.');
    }

    setState(() => _busy = true);
    try {
      if (_mode == _Mode.forgot) {
        await supabase.auth.resetPasswordForEmail(email, redirectTo: kRedirect);
        _info = 'If an account exists for $email, a reset link is on its way.';
      } else if (_mode == _Mode.signUp) {
        final r = await supabase.auth.signUp(
          email: email,
          password: pw,
          emailRedirectTo: kRedirect,
          data: {'full_name': _name.text.trim()},
        );
        if (r.user != null && (r.user!.identities ?? []).isEmpty) {
          return _fail('An account with this email already exists. Try signing in.');
        }
        if (r.session == null) {
          _info = 'Check $email for a confirmation link, then sign in.';
          _mode = _Mode.signIn;
        }
      } else {
        await supabase.auth.signInWithPassword(email: email, password: pw);
      }
    } on AuthException catch (e) {
      return _fail(_friendly(e.message));
    } catch (_) {
      return _fail('Network problem. Check your connection and try again.');
    }
    if (mounted) setState(() => _busy = false);
  }

  Future<void> _google() async {
    try {
      final started = await connectGoogle();
      if (!started) _fail('Could not open Google sign-in.');
    } catch (_) {
      _fail('Could not open Google sign-in.');
    }
  }

  ({double pct, String label, Color color}) _strength(String p) {
    var sc = 0;
    if (p.length >= 8) sc++;
    if (p.length >= 12) sc++;
    if (RegExp(r'[a-z]').hasMatch(p) && RegExp(r'[A-Z]').hasMatch(p)) sc++;
    if (RegExp(r'\d').hasMatch(p)) sc++;
    if (RegExp(r'[^A-Za-z0-9]').hasMatch(p)) sc++;
    const labels = ['Too weak', 'Weak', 'Fair', 'Good', 'Strong', 'Strong'];
    final color = sc <= 1 ? kRed : (sc == 2 ? kAmber : kGreen);
    if (p.isEmpty) return (pct: 0, label: 'Use 8+ characters with letters and numbers', color: kMuted);
    return (pct: (sc * 0.2).clamp(0.08, 1.0), label: labels[sc], color: color);
  }

  Widget _msg(String text, Color c) => Container(
        width: double.infinity,
        margin: const EdgeInsets.only(bottom: 12),
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: c.withAlpha(30),
          border: Border.all(color: c),
          borderRadius: BorderRadius.circular(12),
        ),
        child: Text(text, style: TextStyle(color: c, fontSize: 13)),
      );

  Widget _field(TextEditingController c, String label,
      {bool password = false, TextInputType? type, List<String>? hints, ValueChanged<String>? onChanged}) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: TextField(
        controller: c,
        obscureText: password && !_show,
        keyboardType: type,
        autofillHints: hints,
        onChanged: onChanged,
        onSubmitted: (_) => _submit(),
        decoration: InputDecoration(
          labelText: label,
          suffixIcon: password
              ? TextButton(
                  onPressed: () => setState(() => _show = !_show),
                  child: Text(_show ? 'Hide' : 'Show'),
                )
              : null,
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final up = _mode == _Mode.signUp;
    final forgot = _mode == _Mode.forgot;
    final st = _strength(_pw.text);
    final title = forgot ? 'Reset your password' : (up ? 'Create your account' : 'Welcome back');
    final sub = forgot
        ? 'Enter your email and we will send you a reset link.'
        : (up ? 'Control your mirror from anywhere.' : 'Sign in to your mirror.');

    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Container(
                    width: 52,
                    height: 52,
                    alignment: Alignment.center,
                    decoration: BoxDecoration(
                      borderRadius: BorderRadius.circular(16),
                      gradient: const LinearGradient(colors: [kCyan, Color(0xFF7C3AED)]),
                    ),
                    child: const Text('◐', style: TextStyle(fontSize: 26, color: Color(0xFF04141A))),
                  ),
                  const SizedBox(height: 14),
                  Text(title, style: const TextStyle(fontSize: 26, fontWeight: FontWeight.w800)),
                  const SizedBox(height: 4),
                  Text(sub, style: const TextStyle(color: kMuted)),
                  const SizedBox(height: 20),
                  if (!forgot) ...[
                    OutlinedButton(
                      style: OutlinedButton.styleFrom(
                        backgroundColor: Colors.white,
                        foregroundColor: const Color(0xFF1F1F1F),
                        minimumSize: const Size.fromHeight(50),
                        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
                      ),
                      onPressed: _busy ? null : _google,
                      child: Row(
                        mainAxisAlignment: MainAxisAlignment.center,
                        children: [
                          const Text('G',
                              style: TextStyle(color: Color(0xFF4285F4), fontWeight: FontWeight.w900, fontSize: 20)),
                          const SizedBox(width: 10),
                          Text(up ? 'Sign up with Google' : 'Continue with Google',
                              style: const TextStyle(fontWeight: FontWeight.w600)),
                        ],
                      ),
                    ),
                    const SizedBox(height: 6),
                    const Text('Google also connects your Calendar and Tasks.',
                        textAlign: TextAlign.center, style: TextStyle(color: kMuted, fontSize: 12)),
                    const SizedBox(height: 14),
                    const Row(children: [
                      Expanded(child: Divider(color: kBorder)),
                      Padding(
                        padding: EdgeInsets.symmetric(horizontal: 10),
                        child: Text('or use email', style: TextStyle(color: kMuted, fontSize: 12)),
                      ),
                      Expanded(child: Divider(color: kBorder)),
                    ]),
                    const SizedBox(height: 14),
                    SegmentedButton<_Mode>(
                      segments: const [
                        ButtonSegment(value: _Mode.signIn, label: Text('Sign in')),
                        ButtonSegment(value: _Mode.signUp, label: Text('Create account')),
                      ],
                      selected: {_mode},
                      onSelectionChanged: (s) => _setMode(s.first),
                    ),
                    const SizedBox(height: 16),
                  ],
                  if (_error != null) _msg(_error!, kRed),
                  if (_info != null) _msg(_info!, kGreen),
                  if (up) _field(_name, 'Full name', hints: const [AutofillHints.name]),
                  _field(_email, 'Email', type: TextInputType.emailAddress, hints: const [AutofillHints.email]),
                  if (!forgot)
                    _field(_pw, 'Password',
                        password: true,
                        hints: [up ? AutofillHints.newPassword : AutofillHints.password],
                        onChanged: (_) => setState(() {})),
                  if (up) ...[
                    ClipRRect(
                      borderRadius: BorderRadius.circular(3),
                      child: LinearProgressIndicator(
                        value: st.pct,
                        minHeight: 5,
                        backgroundColor: kCard,
                        valueColor: AlwaysStoppedAnimation(st.color),
                      ),
                    ),
                    const SizedBox(height: 4),
                    Text(st.label, style: const TextStyle(color: kMuted, fontSize: 12)),
                    const SizedBox(height: 12),
                    _field(_pw2, 'Confirm password', password: true),
                  ],
                  FilledButton(
                    style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(50)),
                    onPressed: _busy ? null : _submit,
                    child: Text(_busy
                        ? 'Please wait…'
                        : (forgot ? 'Send reset link' : (up ? 'Create account' : 'Sign in'))),
                  ),
                  const SizedBox(height: 8),
                  if (forgot)
                    TextButton(onPressed: () => _setMode(_Mode.signIn), child: const Text('Back to sign in'))
                  else if (up)
                    const Text('By creating an account you agree to our Terms and Privacy Policy.',
                        textAlign: TextAlign.center, style: TextStyle(color: kMuted, fontSize: 12))
                  else
                    TextButton(onPressed: () => _setMode(_Mode.forgot), child: const Text('Forgot password?')),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
