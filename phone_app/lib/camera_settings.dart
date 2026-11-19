import 'package:shared_preferences/shared_preferences.dart';

// A small setting stored on the phone, so it survives closing the app.
class CameraSettings {
  static const _streamUrlKey = 'camera_stream_url';
  final _preferences = SharedPreferencesAsync();

  Future<String?> loadStreamUrl() => _preferences.getString(_streamUrlKey);

  Future<void> saveStreamUrl(String url) =>
      _preferences.setString(_streamUrlKey, url);
}

// Checks the address format. A correctly formatted camera can still be offline.
String? validateStreamUrl(String? value) {
  final address = value?.trim() ?? '';
  if (address.isEmpty) {
    return 'Enter your camera stream address.';
  }
  if (RegExp(r'\s').hasMatch(address)) {
    return 'Remove spaces from inside the address.';
  }

  try {
    final uri = Uri.parse(address);
    if ((uri.scheme != 'http' && uri.scheme != 'https') || uri.host.isEmpty) {
      return 'Use a full address starting with http:// or https://.';
    }
    if (uri.userInfo.isNotEmpty || uri.hasFragment) {
      return 'Use an address without login details or a # fragment.';
    }
    if (uri.port < 1 || uri.port > 65535) {
      return 'The port number must be between 1 and 65535.';
    }
  } on FormatException {
    return 'Enter a valid camera stream address.';
  }
  return null;
}
