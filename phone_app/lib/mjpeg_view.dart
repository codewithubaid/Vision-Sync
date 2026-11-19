import 'dart:async';
import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

enum StreamStatus { connecting, live, reconnecting }

/// Plays an MJPEG stream (multipart/x-mixed-replace) with no extra packages.
/// It finds each JPEG by its start (FFD8) and end (FFD9) markers, shows the
/// newest frame, and reconnects on its own if the stream drops or stalls.
class MjpegView extends StatefulWidget {
  final String url;
  final BoxFit fit;
  final ValueChanged<StreamStatus>? onStatus;
  const MjpegView({super.key, required this.url, this.fit = BoxFit.contain, this.onStatus});

  @override
  State<MjpegView> createState() => _MjpegViewState();
}

class _MjpegViewState extends State<MjpegView> {
  http.Client? _client;
  StreamSubscription<List<int>>? _sub;
  Timer? _watchdog;
  Timer? _retry;
  Uint8List? _frame;
  Uint8List _buf = Uint8List(0);
  StreamStatus _status = StreamStatus.connecting;
  DateTime _lastFrame = DateTime.now();
  bool _disposed = false;

  @override
  void initState() {
    super.initState();
    _connect();
    // Tell the parent we start in "connecting" (otherwise it can keep an old LIVE badge).
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!_disposed) widget.onStatus?.call(_status);
    });
    // If no frame arrives for 8 seconds, drop the connection and retry. This also
    // covers a server that answers but never sends a picture (_sub != null, no frame yet).
    _watchdog = Timer.periodic(const Duration(seconds: 2), (_) {
      if (_sub != null && DateTime.now().difference(_lastFrame).inSeconds >= 8) {
        _reconnect();
      }
    });
  }

  @override
  void didUpdateWidget(MjpegView old) {
    super.didUpdateWidget(old);
    if (old.url != widget.url) _reconnect();
  }

  @override
  void dispose() {
    _disposed = true;
    _watchdog?.cancel();
    _retry?.cancel();
    _close();
    super.dispose();
  }

  void _close() {
    _sub?.cancel();
    _sub = null;
    _client?.close();
    _client = null;
    _buf = Uint8List(0);
  }

  void _setStatus(StreamStatus s) {
    if (_disposed || s == _status) return;
    setState(() => _status = s);
    widget.onStatus?.call(s);
  }

  void _reconnect() {
    if (_disposed) return;
    _close();
    _setStatus(StreamStatus.reconnecting);
    _retry?.cancel();
    _retry = Timer(const Duration(seconds: 3), _connect);
  }

  Future<void> _connect() async {
    if (_disposed) return;
    _close();
    _lastFrame = DateTime.now();
    final client = http.Client();
    _client = client;
    try {
      final req = http.Request('GET', Uri.parse(widget.url));
      final res = await client.send(req).timeout(const Duration(seconds: 10));
      if (_disposed || _client != client) return;
      if (res.statusCode != 200) {
        _reconnect();
        return;
      }
      _lastFrame = DateTime.now();
      _sub = res.stream.listen(_onChunk, onError: (_) => _reconnect(), onDone: _reconnect, cancelOnError: true);
    } catch (_) {
      if (!_disposed && _client == client) _reconnect();
    }
  }

  static int _find(Uint8List b, int a, int c, int from) {
    for (var i = from; i < b.length - 1; i++) {
      if (b[i] == a && b[i + 1] == c) return i;
    }
    return -1;
  }

  void _onChunk(List<int> chunk) {
    final nb = Uint8List(_buf.length + chunk.length)
      ..setAll(0, _buf)
      ..setAll(_buf.length, chunk);
    _buf = nb;

    Uint8List? latest;
    while (true) {
      final s = _find(_buf, 0xFF, 0xD8, 0);
      if (s < 0) {
        _buf = _buf.length > 1 ? Uint8List.fromList([_buf.last]) : _buf;
        break;
      }
      final e = _find(_buf, 0xFF, 0xD9, s + 2);
      if (e < 0) {
        if (s > 0) _buf = Uint8List.fromList(_buf.sublist(s));
        break;
      }
      latest = Uint8List.fromList(_buf.sublist(s, e + 2));
      _buf = Uint8List.fromList(_buf.sublist(e + 2));
    }
    if (_buf.length > 4 * 1024 * 1024) _buf = Uint8List(0); // safety: never grow forever

    if (latest != null && !_disposed) {
      _lastFrame = DateTime.now();
      setState(() => _frame = latest);
      _setStatus(StreamStatus.live);
    }
  }

  @override
  Widget build(BuildContext context) {
    final f = _frame;
    if (f == null) {
      return Center(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          const CircularProgressIndicator(),
          const SizedBox(height: 12),
          Text(_status == StreamStatus.reconnecting ? 'Reconnecting…' : 'Connecting to camera…',
              style: const TextStyle(color: Colors.white70)),
        ]),
      );
    }
    return Stack(fit: StackFit.expand, children: [
      Image.memory(f, fit: widget.fit, gaplessPlayback: true),
      if (_status == StreamStatus.reconnecting)
        const Align(
          alignment: Alignment.bottomCenter,
          child: Padding(
            padding: EdgeInsets.all(8),
            child: Text('Connection lost. Reconnecting…', style: TextStyle(color: Colors.amber)),
          ),
        ),
    ]);
  }
}
