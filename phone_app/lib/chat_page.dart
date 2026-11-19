import 'package:flutter/material.dart';
import 'connection.dart';
import 'mirror_api.dart';
import 'models.dart';
import 'theme.dart';

class ChatMessage {
  final bool fromUser;
  final String text;
  final bool isError; // error notices are shown but not sent back as chat history
  ChatMessage(this.fromUser, this.text, {this.isError = false});
}

/// Chat with the mirror's assistant. The answers come from the camera server (/chat).
class ChatPage extends StatefulWidget {
  final Mirror mirror;
  const ChatPage({super.key, required this.mirror});

  @override
  State<ChatPage> createState() => _ChatPageState();
}

class _ChatPageState extends State<ChatPage> {
  final List<ChatMessage> _msgs = [
    ChatMessage(false, 'Hi! Ask me about your mirror: alerts, Security Mode, the camera, temperature and more.'),
  ];
  final TextEditingController _input = TextEditingController();
  final ScrollController _scroll = ScrollController();
  bool _sending = false;
  String _mode = ''; // 'cloud' or 'local', shown at the top

  static const _suggestions = ['Any alerts?', 'Is Security Mode on?', 'Is the camera on?', 'Status'];

  @override
  void dispose() {
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  void _toEnd() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scroll.hasClients) {
        _scroll.animateTo(_scroll.position.maxScrollExtent, duration: const Duration(milliseconds: 250), curve: Curves.easeOut);
      }
    });
  }

  Future<void> _send([String? preset]) async {
    final text = (preset ?? _input.text).trim();
    if (text.isEmpty || _sending) return;
    _input.clear();

    // Earlier messages (without the first greeting) give the assistant some memory.
    final history = _msgs
        .skip(1)
        .where((m) => !m.isError)
        .map((m) => <String, String>{'role': m.fromUser ? 'user' : 'assistant', 'text': m.text})
        .toList();

    setState(() {
      _msgs.add(ChatMessage(true, text));
      _sending = true;
    });
    _toEnd();

    String reply;
    bool failed = false;
    try {
      final r = await MirrorApi(widget.mirror).chat(text, history);
      reply = r.reply.isEmpty ? '(no answer)' : r.reply;
      _mode = r.mode;
    } catch (e) {
      reply = (await diagnoseError(e, widget.mirror)).message;
      failed = true;
    }
    if (!mounted) return;
    setState(() {
      _msgs.add(ChatMessage(false, reply, isError: failed));
      _sending = false;
    });
    _toEnd();
  }

  Widget _bubble(ChatMessage m) {
    return Align(
      alignment: m.fromUser ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        margin: const EdgeInsets.symmetric(vertical: 4),
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        constraints: BoxConstraints(maxWidth: MediaQuery.of(context).size.width * 0.8),
        decoration: BoxDecoration(
          color: m.fromUser ? kCyan : kCard,
          borderRadius: BorderRadius.circular(16),
        ),
        child: Text(m.text, style: TextStyle(color: m.fromUser ? Colors.black : Colors.white, fontSize: 15)),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Text('Assistant', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 16)),
          if (_mode.isNotEmpty)
            Text(_mode == 'cloud' ? 'Online AI' : 'Offline mode (basic answers)',
                style: const TextStyle(fontSize: 12, color: kMuted)),
        ]),
      ),
      body: SafeArea(
        child: Column(children: [
          Expanded(
            child: ListView(
              controller: _scroll,
              padding: const EdgeInsets.all(14),
              children: [
                ..._msgs.map(_bubble),
                if (_sending)
                  const Align(
                    alignment: Alignment.centerLeft,
                    child: Padding(
                      padding: EdgeInsets.all(8),
                      child: Text('Typing…', style: TextStyle(color: kMuted)),
                    ),
                  ),
              ],
            ),
          ),
          if (_msgs.length == 1)
            SizedBox(
              height: 44,
              child: ListView(
                scrollDirection: Axis.horizontal,
                padding: const EdgeInsets.symmetric(horizontal: 12),
                children: _suggestions
                    .map((s) => Padding(
                          padding: const EdgeInsets.only(right: 8),
                          child: ActionChip(label: Text(s), onPressed: () => _send(s)),
                        ))
                    .toList(),
              ),
            ),
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 6, 12, 10),
            child: Row(children: [
              Expanded(
                child: TextField(
                  controller: _input,
                  textInputAction: TextInputAction.send,
                  onSubmitted: (_) => _send(),
                  decoration: const InputDecoration(hintText: 'Ask something…', border: OutlineInputBorder()),
                ),
              ),
              const SizedBox(width: 8),
              IconButton.filled(onPressed: _sending ? null : () => _send(), icon: const Icon(Icons.send)),
            ]),
          ),
        ]),
      ),
    );
  }
}
