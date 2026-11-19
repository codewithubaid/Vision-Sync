import 'dart:async';

import 'package:glass_connect/camera_settings.dart';
import 'package:glass_connect/direct_camera_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

// Widget tests use a pretend camera and storage, without contacting the laptop.
class FakeCameraSettings implements CameraSettings {
  FakeCameraSettings({this.savedUrl});

  String? savedUrl;
  bool failLoading = false;
  bool failSaving = false;
  int saveCount = 0;
  Completer<void>? pendingSave;

  @override
  Future<String?> loadStreamUrl() async {
    if (failLoading) throw StateError('Storage unavailable');
    return savedUrl;
  }

  @override
  Future<void> saveStreamUrl(String url) async {
    saveCount++;
    if (failSaving) throw StateError('Storage unavailable');
    if (pendingSave != null) await pendingSave!.future;
    savedUrl = url;
  }
}

Widget preview(String url) => Text('Preview: $url');

Future<void> openApp(WidgetTester tester, FakeCameraSettings settings) async {
  await tester.pumpWidget(MaterialApp(
    home: LiveFeedScreen(settings: settings, streamBuilder: preview),
  ));
  await tester.pumpAndSettle();
}

void main() {
  test('validates full HTTP camera addresses without restricting the host', () {
    for (final address in [
      defaultCameraStreamUrl,
      '  http://192.168.1.10:5000/video_feed  ',
      'https://mirror.local/camera?quality=70',
      'http://[::1]:5000/video_feed',
    ]) {
      expect(validateStreamUrl(address), isNull, reason: address);
    }

    for (final address in [
      '',
      '   ',
      '192.168.1.10:5000/video_feed',
      'rtsp://192.168.1.10/video_feed',
      'http:///video_feed',
      'http://192.168. 1.10/video_feed',
      'http://localhost:wrong/video_feed',
      'http://localhost:0/video_feed',
      'http://localhost:65536/video_feed',
      'http://[broken/video_feed',
      'http://user:password@mirror.local/video_feed',
      'http://mirror.local/video_feed#fragment',
    ]) {
      expect(validateStreamUrl(address), isNotNull, reason: address);
    }
  });

  testWidgets('first launch keeps the existing camera address', (tester) async {
    await openApp(tester, FakeCameraSettings());

    expect(find.text('Preview: $defaultCameraStreamUrl'), findsOneWidget);
    expect(
      find.widgetWithText(FilledButton, 'Save and connect'),
      findsOneWidget,
    );
  });

  testWidgets('loads a saved address into the form and viewer', (tester) async {
    const saved = 'http://192.168.1.20:5000/video_feed';
    await openApp(tester, FakeCameraSettings(savedUrl: saved));

    expect(find.text('Preview: $saved'), findsOneWidget);
    expect(
      tester.widget<TextFormField>(find.byType(TextFormField)).controller!.text,
      saved,
    );
  });

  testWidgets('saves a trimmed address and restores it on the next launch', (
    tester,
  ) async {
    final settings = FakeCameraSettings();
    const newAddress = 'http://192.168.1.30:5000/video_feed';
    await openApp(tester, settings);

    await tester.enterText(find.byType(TextFormField), '  $newAddress  ');
    await tester.tap(find.text('Save and connect'));
    await tester.pumpAndSettle();

    expect(settings.savedUrl, newAddress);
    expect(find.text('Preview: $newAddress'), findsOneWidget);

    await tester.pumpWidget(const SizedBox.shrink());
    await openApp(tester, settings);
    expect(find.text('Preview: $newAddress'), findsOneWidget);
  });

  testWidgets('invalid input leaves the saved address and viewer unchanged', (
    tester,
  ) async {
    final settings = FakeCameraSettings(savedUrl: defaultCameraStreamUrl);
    await openApp(tester, settings);

    await tester.enterText(find.byType(TextFormField), 'not a camera address');
    await tester.tap(find.text('Save and connect'));
    await tester.pumpAndSettle();

    expect(settings.saveCount, 0);
    expect(settings.savedUrl, defaultCameraStreamUrl);
    expect(find.text('Preview: $defaultCameraStreamUrl'), findsOneWidget);
    expect(find.text('Remove spaces from inside the address.'), findsOneWidget);
  });

  testWidgets('failed save shows an error and keeps the current camera', (
    tester,
  ) async {
    final settings = FakeCameraSettings(savedUrl: defaultCameraStreamUrl)
      ..failSaving = true;
    await openApp(tester, settings);

    await tester.enterText(
      find.byType(TextFormField),
      'http://192.168.1.40:5000/video_feed',
    );
    await tester.tap(find.text('Save and connect'));
    await tester.pumpAndSettle();

    expect(settings.savedUrl, defaultCameraStreamUrl);
    expect(find.text('Preview: $defaultCameraStreamUrl'), findsOneWidget);
    expect(
      find.text('Could not save the address. Please try again.'),
      findsOneWidget,
    );
    expect(find.text('Address saved. Opening camera stream.'), findsNothing);
  });

  testWidgets('failed load offers recovery without leaving a loading spinner', (
    tester,
  ) async {
    await openApp(tester, FakeCameraSettings()..failLoading = true);

    expect(find.byType(CircularProgressIndicator), findsNothing);
    expect(find.text('Preview: $defaultCameraStreamUrl'), findsOneWidget);
    expect(
      find.text('Could not load your saved address. Enter and save it again.'),
      findsOneWidget,
    );
  });

  testWidgets('saving disables repeat submissions until storage finishes', (
    tester,
  ) async {
    final pending = Completer<void>();
    final settings = FakeCameraSettings()..pendingSave = pending;
    await openApp(tester, settings);

    await tester.tap(find.text('Save and connect'));
    await tester.pump();
    expect(
      tester.widget<FilledButton>(find.byType(FilledButton)).onPressed,
      isNull,
    );
    expect(settings.saveCount, 1);

    pending.complete();
    await tester.pumpAndSettle();
    expect(
      tester.widget<FilledButton>(find.byType(FilledButton)).onPressed,
      isNotNull,
    );
  });

  testWidgets('reconnect recreates the saved stream without applying edits', (
    tester,
  ) async {
    final settings = FakeCameraSettings();
    await openApp(tester, settings);
    final originalPreview = tester.element(
      find.text('Preview: $defaultCameraStreamUrl'),
    );
    await tester.enterText(
      find.byType(TextFormField),
      'http://192.168.1.50:5000/video_feed',
    );

    await tester.tap(find.byTooltip('Reconnect to saved camera'));
    await tester.pumpAndSettle();

    expect(originalPreview.mounted, isFalse);
    expect(find.text('Preview: $defaultCameraStreamUrl'), findsOneWidget);
    expect(settings.saveCount, 0);
  });
}
