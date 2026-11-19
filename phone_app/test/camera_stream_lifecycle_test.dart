import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:mjpeg_stream/mjpeg_stream.dart';

class PendingCameraClient extends http.BaseClient {
  final response = Completer<http.StreamedResponse>();
  bool closed = false;

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) =>
      response.future;

  @override
  void close() => closed = true;
}

Widget cameraWith(http.Client Function() clientFactory) => MaterialApp(
  home: Scaffold(
    body: MJPEGStreamScreen(
      streamUrl: 'http://camera.test/video_feed',
      showLiveIcon: true,
      showLogs: false,
      clientFactory: clientFactory,
    ),
  ),
);

void main() {
  testWidgets('closing the viewer safely handles a late request failure', (
    tester,
  ) async {
    final client = PendingCameraClient();
    await tester.pumpWidget(cameraWith(() => client));
    await tester.pumpWidget(const SizedBox.shrink());

    expect(client.closed, isTrue);
    client.response.completeError(StateError('Request cancelled'));
    await tester.pump();
    expect(tester.takeException(), isNull);
  });

  testWidgets('closing the viewer ignores a late successful response', (
    tester,
  ) async {
    final client = PendingCameraClient();
    await tester.pumpWidget(cameraWith(() => client));
    await tester.pumpWidget(const SizedBox.shrink());

    client.response.complete(
      http.StreamedResponse(const Stream<List<int>>.empty(), 200),
    );
    await tester.pump();
    expect(client.closed, isTrue);
    expect(tester.takeException(), isNull);
  });

  testWidgets('retry replaces the old request and stream end clears LIVE', (
    tester,
  ) async {
    final first = PendingCameraClient();
    final second = PendingCameraClient();
    var connection = 0;
    await tester.pumpWidget(
      cameraWith(() => connection++ == 0 ? first : second),
    );
    first.response.complete(
      http.StreamedResponse(const Stream<List<int>>.empty(), 503),
    );
    await tester.pumpAndSettle();
    expect(find.text('Stream Error'), findsOneWidget);

    await tester.tap(find.text('Retry'));
    await tester.pump();
    expect(first.closed, isTrue);

    final frames = StreamController<List<int>>();
    second.response.complete(http.StreamedResponse(frames.stream, 200));
    await tester.pump();
    frames.add([]);
    await tester.pump();
    expect(tester.takeException(), isNull);

    await frames.close();
    await tester.pumpAndSettle();
    expect(find.text('LIVE'), findsNothing);
    expect(find.text('Stream Error'), findsOneWidget);
    expect(second.closed, isTrue);
    expect(tester.takeException(), isNull);
  });
}
