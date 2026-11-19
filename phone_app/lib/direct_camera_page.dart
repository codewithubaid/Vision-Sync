// Direct camera stream screen, taken unchanged from the VisionSync camera project
// (its lib/main.dart). Only the partner's own main() and MaterialApp (MyApp) were
// removed so the merged app keeps ONE entry point (lib/main.dart). It is opened from
// Settings > "Direct camera stream" in home_page.dart.
import 'package:flutter/material.dart';
import 'package:mjpeg_stream/mjpeg_stream.dart';

import 'camera_settings.dart';

// Used on the first launch. After that, the app loads the saved address.
// The mirror now serves this on its main port (8080) and it needs the mirror secret:
//   http://<mirror address>:8080/video_feed?token=<MIRROR_SECRET>
// Replace the address and the token in the app, then tap "Save and connect".
const String defaultCameraStreamUrl = 'http://192.168.1.10:8080/video_feed?token=YOUR_SECRET';

typedef CameraStreamBuilder = Widget Function(String streamUrl);

Widget buildCameraStream(String streamUrl) => MJPEGStreamScreen(
  streamUrl: streamUrl,
  showLiveIcon: true,
  showLogs: false,
);

class LiveFeedScreen extends StatefulWidget {
  const LiveFeedScreen({
    super.key,
    this.settings,
    this.streamBuilder = buildCameraStream,
  });

  final CameraSettings? settings;
  final CameraStreamBuilder streamBuilder;

  @override
  State<LiveFeedScreen> createState() => _LiveFeedScreenState();
}

class _LiveFeedScreenState extends State<LiveFeedScreen> {
  final _formKey = GlobalKey<FormState>();
  final _addressController = TextEditingController();
  late final CameraSettings _settings;
  String _streamUrl = defaultCameraStreamUrl;
  String? _settingsError;
  bool _loading = true;
  bool _saving = false;
  int _connectionNumber = 0;

  @override
  void initState() {
    super.initState();
    _settings = widget.settings ?? CameraSettings();
    _loadSettings();
  }

  Future<void> _loadSettings() async {
    var address = defaultCameraStreamUrl;
    String? error;
    try {
      final savedAddress = await _settings.loadStreamUrl();
      if (savedAddress != null) {
        if (validateStreamUrl(savedAddress) == null) {
          address = savedAddress.trim();
        } else {
          error = 'The saved address is invalid. Enter and save a new address.';
        }
      }
    } catch (_) {
      error = 'Could not load your saved address. Enter and save it again.';
    }

    if (!mounted) return;
    setState(() {
      _streamUrl = address;
      _addressController.text = address;
      _settingsError = error;
      _loading = false;
    });
  }

  Future<void> _saveAndConnect() async {
    if (_loading || _saving || !_formKey.currentState!.validate()) return;
    FocusScope.of(context).unfocus();
    final address = _addressController.text.trim();
    setState(() {
      _saving = true;
      _settingsError = null;
    });

    try {
      await _settings.saveStreamUrl(address);
      if (!mounted) return;
      setState(() {
        _streamUrl = address;
        _addressController.text = address;
        // A new key recreates the viewer, including when retrying the same URL.
        _connectionNumber++;
      });
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Address saved. Opening camera stream.')),
      );
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _settingsError = 'Could not save the address. Please try again.';
      });
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  void dispose() {
    _addressController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Live Camera Feed'),
        actions: [
          IconButton(
            tooltip: 'Reconnect to saved camera',
            onPressed: _loading || _saving
                ? null
                : () => setState(() => _connectionNumber++),
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : ListView(
              padding: const EdgeInsets.all(16),
              children: [
                Form(
                  key: _formKey,
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      TextFormField(
                        controller: _addressController,
                        enabled: !_saving,
                        keyboardType: TextInputType.url,
                        autocorrect: false,
                        enableSuggestions: false,
                        textInputAction: TextInputAction.done,
                        decoration: const InputDecoration(
                          labelText: 'Camera stream address',
                          hintText: 'http://192.168.1.10:8080/video_feed?token=YOUR_SECRET',
                          border: OutlineInputBorder(),
                          errorMaxLines: 3,
                        ),
                        validator: validateStreamUrl,
                        onFieldSubmitted: (_) => _saveAndConnect(),
                      ),
                      const SizedBox(height: 8),
                      const Text(
                        'Run the camera server on your laptop and connect both '
                        'devices to the same Wi-Fi.',
                      ),
                      const SizedBox(height: 12),
                      FilledButton.icon(
                        onPressed: _saving ? null : _saveAndConnect,
                        icon: const Icon(Icons.videocam),
                        label: Text(_saving ? 'Saving...' : 'Save and connect'),
                      ),
                      if (_settingsError != null) ...[
                        const SizedBox(height: 8),
                        Text(
                          _settingsError!,
                          style: TextStyle(
                            color: Theme.of(context).colorScheme.error,
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
                const SizedBox(height: 16),
                Text('Stream address: $_streamUrl'),
                const SizedBox(height: 8),
                AspectRatio(
                  aspectRatio: 4 / 3,
                  child: KeyedSubtree(
                    key: ValueKey(_connectionNumber),
                    child: widget.streamBuilder(_streamUrl),
                  ),
                ),
              ],
            ),
    );
  }
}
