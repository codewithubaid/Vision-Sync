/*
 * MMM-VisionSync - MagicMirror2 module for the VisionSync smart mirror.
 *
 * One module, used several times in config.js, each time with a different
 * `widget`:  clock | weather | calendar | status | guard | assistant | alerts | ambient | setup
 *
 * All data comes from the VisionSync hub (Python) through node_helper.js over
 * a local Unix socket (SRS 4.4). The clock runs from the mirror's own time, so
 * it works even when the hub and the network are down.
 */
/* global Module, VSWidgets */
Module.register("MMM-VisionSync", {
  defaults: {
    widget: "clock",
    socketPath: "/tmp/visionsync.sock",
    timeFormat: null,        // null = use MagicMirror's global timeFormat
    showSeconds: true,
    showForecast: true,
    maxEvents: 5,
    alarmSound: "offline",   // "offline" (SRS UC-3 A4) | "always" | "never"
    ambient: null            // ambient widget: "off" | "subtle" | "vivid"; null = AMBIENT from .env
  },

  getScripts: function () { return [this.file("vs-qr.js"), this.file("vs-widgets.js")]; },
  getStyles: function () { return [this.file("vs.css")]; },

  start: function () {
    this.vsState = null;
    this.vsWidget = null;
    this.vsRoot = null;
    if (this.config.timeFormat === null) { this.config.timeFormat = config.timeFormat === 24 ? 24 : 12; }
    this.sendSocketNotification("VS_CONNECT", { socketPath: this.config.socketPath });
  },

  // Built once. After that the widget updates its own element, so MagicMirror
  // never tears the DOM down and the animations stay smooth on the Jetson.
  getDom: function () {
    if (!this.vsRoot) {
      this.vsRoot = document.createElement("div");
      this.vsWidget = VSWidgets.mount(this.config.widget, this.vsRoot, this.config);
      this.vsWidget.update(this.vsState);
    }
    return this.vsRoot;
  },

  socketNotificationReceived: function (notification, payload) {
    if (notification === "VS_STATE") {
      this.vsState = payload;
    } else if (notification === "VS_LINK") {
      // Hub stopped: keep the last data on screen, but mark it as old.
      this.vsState = Object.assign({}, this.vsState || {}, { _link: payload.connected });
    } else {
      return;
    }
    VSWidgets.applyThrottle(this.vsState);
    if (this.vsWidget) { this.vsWidget.update(this.vsState); }
  },

  suspend: function () {},
  resume: function () { if (this.vsWidget) { this.vsWidget.update(this.vsState); } }
});
