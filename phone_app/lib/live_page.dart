import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'config.dart';
import 'mjpeg_view.dart';
import 'models.dart';
import 'theme.dart';

/// Live camera tab. The video comes straight from the mirror through Tailscale.
class LivePage extends StatefulWidget {
  final Mirror mirror;
  final bool camOn;
  final String? problem; // e.g. "can't reach the camera"; null when all is fine
  const LivePage({super.key, required this.mirror, required this.camOn, this.problem});

  @override
  State<LivePage> createState() => _LivePageState();
}

class _LivePageState extends State<LivePage> with WidgetsBindingObserver {
  bool _inForeground = true;
  bool _paused = false;
  bool _fs = false; // full screen is open: the small player must not also stream
  StreamStatus _status = StreamStatus.connecting;

  String get _url => streamUrl(widget.mirror);

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  // Stop pulling video when the app is in the background (saves battery and mobile data).
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    setState(() => _inForeground = state == AppLifecycleState.resumed);
  }

  Future<void> _fullscreen() async {
    await SystemChrome.setPreferredOrientations(DeviceOrientation.values);
    await SystemChrome.setEnabledSystemUIMode(SystemUiMode.immersiveSticky);
    if (!mounted) return;
    setState(() => _fs = true);
    await Navigator.of(context).push(MaterialPageRoute(builder: (_) => _FullscreenLive(url: _url)));
    if (mounted) setState(() => _fs = false);
    await SystemChrome.setPreferredOrientations([DeviceOrientation.portraitUp]);
    await SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);
  }

  Widget _badge() {
    final live = _status == StreamStatus.live;
    final c = live ? kRed : kAmber;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(color: Colors.black54, borderRadius: BorderRadius.circular(8)),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        Icon(Icons.circle, size: 10, color: c),
        const SizedBox(width: 6),
        Text(live ? 'LIVE' : (_status == StreamStatus.connecting ? 'CONNECTING' : 'RECONNECTING'),
            style: TextStyle(color: c, fontWeight: FontWeight.w800, fontSize: 11)),
      ]),
    );
  }

  @override
  Widget build(BuildContext context) {
    final streaming = widget.camOn && widget.problem == null && _inForeground && !_paused && !_fs;
    String? msg;
    if (!widget.camOn) {
      msg = 'Camera is off. Turn it on in Settings to watch live.';
    } else if (widget.problem != null) {
      msg = widget.problem;
    } else if (_fs) {
      msg = 'Playing full screen';
    } else if (_paused) {
      msg = 'Paused';
    }

    return ListView(padding: const EdgeInsets.all(14), children: [
      ClipRRect(
        borderRadius: BorderRadius.circular(16),
        child: AspectRatio(
          aspectRatio: 16 / 9,
          child: Container(
            color: Colors.black,
            child: Stack(fit: StackFit.expand, children: [
              if (streaming)
                MjpegView(
                  key: ValueKey(_url),
                  url: _url,
                  onStatus: (s) {
                    if (mounted) setState(() => _status = s);
                  },
                )
              else
                Center(
                  child: Padding(
                    padding: const EdgeInsets.all(20),
                    child: Column(mainAxisSize: MainAxisSize.min, children: [
                      Icon(!widget.camOn ? Icons.videocam_off : (widget.problem != null ? Icons.wifi_off : Icons.pause_circle_outline), size: 40, color: kMuted),
                      const SizedBox(height: 8),
                      Text(msg ?? '', textAlign: TextAlign.center, style: const TextStyle(color: kMuted)),
                    ]),
                  ),
                ),
              if (streaming) Positioned(top: 10, left: 10, child: _badge()),
            ]),
          ),
        ),
      ),
      if (streaming && _status == StreamStatus.reconnecting)
        const Padding(
          padding: EdgeInsets.only(top: 12),
          child: GCard(
            border: kAmber,
            child: Text(
              'Stream unavailable. The mirror answers, but the video did not start. Retrying automatically. If this stays, check that the camera is on and the camera program is running.',
              style: TextStyle(color: kAmber, fontSize: 13),
            ),
          ),
        ),
      const SizedBox(height: 12),
      Row(children: [
        Expanded(
          child: OutlinedButton.icon(
            onPressed: widget.camOn ? () => setState(() => _paused = !_paused) : null,
            icon: Icon(_paused ? Icons.play_arrow : Icons.pause),
            label: Text(_paused ? 'Resume' : 'Pause'),
          ),
        ),
        const SizedBox(width: 10),
        Expanded(
          child: FilledButton.icon(
            onPressed: streaming ? _fullscreen : null,
            icon: const Icon(Icons.fullscreen),
            label: const Text('Full screen'),
          ),
        ),
      ]),
      const SizedBox(height: 12),
      GCard(
        child: Text(
          'Streaming from ${widget.mirror.address} through Tailscale. Choose 480p in Settings on mobile data or a weak connection.',
          style: const TextStyle(color: kMuted, fontSize: 13),
        ),
      ),
    ]);
  }
}

class _FullscreenLive extends StatefulWidget {
  final String url;
  const _FullscreenLive({required this.url});

  @override
  State<_FullscreenLive> createState() => _FullscreenLiveState();
}

class _FullscreenLiveState extends State<_FullscreenLive> with WidgetsBindingObserver {
  bool _inForeground = true;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  // Stop pulling video when the app is in the background, like the small player does.
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    setState(() => _inForeground = state == AppLifecycleState.resumed);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.black,
      body: Stack(children: [
        Positioned.fill(
          child: _inForeground
              ? InteractiveViewer(minScale: 1, maxScale: 4, child: MjpegView(url: widget.url))
              : const SizedBox.shrink(),
        ),
        SafeArea(
          child: Padding(
            padding: const EdgeInsets.all(8),
            child: IconButton(
              style: IconButton.styleFrom(backgroundColor: Colors.black54),
              icon: const Icon(Icons.close, color: Colors.white),
              onPressed: () => Navigator.of(context).pop(),
            ),
          ),
        ),
      ]),
    );
  }
}
