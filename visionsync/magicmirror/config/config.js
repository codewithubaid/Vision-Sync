/* VisionSync - MagicMirror2 configuration
 *
 * Copy to  ~/MagicMirror/config/config.js
 *
 * Layout (SRS 4.1): clock top centre, weather top right, calendar bottom left.
 * Added: system status top left, voice assistant lower third, modes bottom
 * right, and a full-screen layer for security / fall alerts.
 *
 * IMPORTANT: the camera backend (motion_stream_server.py) already uses port
 * 8080 for the phone app, so MagicMirror runs on 8181 here.
 * API keys are NOT in this file. They live in visionsync/.env (NFR-SEC1).
 */
let config = {
  address: "localhost",
  port: 8181,
  basePath: "/",
  ipWhitelist: ["127.0.0.1", "::ffff:127.0.0.1", "::1"],   // mirror only (NFR-SEC2)
  useHttps: false,
  language: "en",
  locale: "en-PK",
  logLevel: ["INFO", "LOG", "WARN", "ERROR"],
  timeFormat: 12,
  units: "metric",

  modules: [
    // Glowing background. ambient: "off" | "subtle" | "vivid"  (leave out to use AMBIENT from .env)
    { module: "MMM-VisionSync", position: "fullscreen_below", config: { widget: "ambient" } },
    { module: "MMM-VisionSync", position: "top_left",         config: { widget: "status" } },
    { module: "MMM-VisionSync", position: "top_center",       config: { widget: "clock", showSeconds: true } },
    { module: "MMM-VisionSync", position: "top_right",        config: { widget: "weather", showForecast: true } },
    { module: "MMM-VisionSync", position: "lower_third",      config: { widget: "assistant" } },
    { module: "MMM-VisionSync", position: "bottom_left",      config: { widget: "calendar", maxEvents: 5 } },
    { module: "MMM-VisionSync", position: "bottom_right",     config: { widget: "agenda" } },
    { module: "MMM-VisionSync", position: "bottom_right",     config: { widget: "guard" } },
    // Wi-Fi setup screen with QR code, shown only while the mirror has no network
    { module: "MMM-VisionSync", position: "fullscreen_above", config: { widget: "setup" } },
    { module: "MMM-VisionSync", position: "fullscreen_above", config: { widget: "alerts", alarmSound: "offline" } }
  ]
};

/*************** DO NOT EDIT THE LINE BELOW ***************/
if (typeof module !== "undefined") { module.exports = config; }
