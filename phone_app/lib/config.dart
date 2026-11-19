import 'package:supabase_flutter/supabase_flutter.dart';
import 'models.dart';

const kSupabaseUrl = 'https://dwoviorikllonqocehpc.supabase.co';
const kSupabaseKey = 'sb_publishable_7aou8WJhnDiV11nY2FWflQ_HWSJnzAF';

/// Deep link Supabase sends the user back to after Google login / email links.
/// Must also be added in Supabase: Authentication > URL Configuration > Redirect URLs.
const kRedirect = 'io.supabase.glassconnect://login-callback/';

// calendar.events: the mirror puts reminders said to Vision in your Google Calendar, and reads it.
const kGoogleScopes =
    'https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/tasks';

SupabaseClient get supabase => Supabase.instance.client;

// ---- Camera on the mirror (motion_stream_server.py) ----
// The app talks to the mirror at the ip and port saved for it (default 8080).
// kMirrorSecret must be EXACTLY the same as MIRROR_SECRET_KEY on the mirror.
// It is NOT stored in the source code: build with
//   --dart-define=MIRROR_SECRET=your-own-secret
// There is no default on purpose. Without it the app says "secret not set" instead of
// silently using a secret that is public in the source.
const kMirrorSecret = String.fromEnvironment('MIRROR_SECRET');

// Optional build-time default for the mirror's Tailscale IP / MagicDNS name, used when
// neither the phone setting nor the Supabase `mirrors.tailscale_host` column has one:
//   --dart-define=MIRROR_HOST=100.101.102.103
const kDefaultMirrorHost = String.fromEnvironment('MIRROR_HOST');

const kStreamPath = '/stream.mjpg';

// Everything goes to the Tailscale address (m.address), never to the LAN ip when a
// Tailscale address is known. The token is a query parameter because the video player
// cannot send headers; it travels inside the encrypted Tailscale tunnel.
String streamUrl(Mirror m) =>
    'http://${m.address}:${m.port}$kStreamPath?token=${Uri.encodeQueryComponent(kMirrorSecret)}';
