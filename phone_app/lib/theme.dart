import 'package:flutter/material.dart';

const kBg = Color(0xFF0A0B0E);
const kCard = Color(0xFF12151C);
const kBorder = Color(0xFF232838);
const kCyan = Color(0xFF00D4FF);
const kRed = Color(0xFFEF4444);
const kAmber = Color(0xFFF59E0B);
const kGreen = Color(0xFF10B981);
const kMuted = Color(0xFF8A93A6);

ThemeData glassTheme() {
  return ThemeData(
    brightness: Brightness.dark,
    useMaterial3: true,
    scaffoldBackgroundColor: kBg,
    colorScheme: const ColorScheme.dark(
      primary: kCyan,
      onPrimary: Color(0xFF04141A),
      surface: kBg,
      error: kRed,
    ),
    appBarTheme: const AppBarTheme(backgroundColor: kBg, elevation: 0),
    navigationBarTheme: const NavigationBarThemeData(
      backgroundColor: kCard,
      indicatorColor: Color(0x3300D4FF),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: kCard,
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(12),
        borderSide: const BorderSide(color: kBorder),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(12),
        borderSide: const BorderSide(color: kBorder),
      ),
    ),
  );
}

class GCard extends StatelessWidget {
  final Widget child;
  final Color? border;
  final EdgeInsets padding;
  const GCard({
    super.key,
    required this.child,
    this.border,
    this.padding = const EdgeInsets.all(14),
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      margin: const EdgeInsets.only(bottom: 12),
      padding: padding,
      decoration: BoxDecoration(
        color: kCard,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: border ?? kBorder),
      ),
      child: child,
    );
  }
}
