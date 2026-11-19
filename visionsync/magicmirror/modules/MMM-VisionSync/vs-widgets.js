/*
 * vs-widgets.js - VisionSync dashboard widgets.
 *
 * Plain JavaScript, no framework, so the same file runs inside the MagicMirror2
 * module and inside the hub's stand-alone page.
 *
 *   var w = VSWidgets.mount("weather", element, options);
 *   w.update(state);          // state = the hub's JSON
 *
 * Widgets: clock, weather, calendar, status, guard, assistant, alerts, ambient, setup.
 * The setup widget needs vs-qr.js loaded first.
 */
(function (root) {
  "use strict";

  var DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  var MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

  // ------------------------------------------------------------ helpers
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function pad(n) { return n < 10 ? "0" + n : "" + n; }
  function fmtTime(d, h24) {
    if (h24) { return pad(d.getHours()) + ":" + pad(d.getMinutes()); }
    var h = d.getHours() % 12 || 12;
    return h + ":" + pad(d.getMinutes()) + (d.getHours() < 12 ? " AM" : " PM");
  }
  function fmtHour(d, h24) {
    if (h24) { return pad(d.getHours()) + ":00"; }
    return (d.getHours() % 12 || 12) + (d.getHours() < 12 ? " AM" : " PM");
  }
  function dayStart(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime(); }
  function dayDiff(a, b) { return Math.round((dayStart(a) - dayStart(b)) / 86400000); }
  function round(v) { return (v == null || isNaN(v)) ? "–" : String(Math.round(v)); }

  // Re-draw only when the content really changed, so animations are not restarted.
  function setHTML(el, html) {
    if (el.__vsHTML !== html) { el.__vsHTML = html; el.innerHTML = html; }
  }
  function setClass(el, cls) {
    if (el.className !== cls) { el.className = cls; }
  }

  // ------------------------------------------------------------ icons
  var P = {
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.5 1.5M17.2 17.2l1.5 1.5M5.3 18.7l1.5-1.5M17.2 6.8l1.5-1.5"/>',
    moon: '<path d="M19.5 14.2A8 8 0 0 1 9.8 4.5a8 8 0 1 0 9.7 9.7z"/>',
    cloud: '<path d="M7 18.5h10a4 4 0 0 0 .6-7.96A6 6 0 0 0 6.1 9.6 4.5 4.5 0 0 0 7 18.5z"/>',
    cloudSun: '<circle cx="8" cy="7.5" r="2.8"/><path d="M8 2.2v1.2M2.7 7.5h1.2M4.2 3.7l.9.9M11.8 3.7l-.9.9"/><path d="M9.5 19.5h8a3.3 3.3 0 0 0 .5-6.56A5 5 0 0 0 8.7 12.1 3.8 3.8 0 0 0 9.5 19.5z"/>',
    cloudMoon: '<path d="M10.8 6.9A4.2 4.2 0 0 1 6 2.6a4.2 4.2 0 1 0 4.8 4.3z"/><path d="M9.5 19.5h8a3.3 3.3 0 0 0 .5-6.56A5 5 0 0 0 8.7 12.1 3.8 3.8 0 0 0 9.5 19.5z"/>',
    rain: '<path d="M7 15.5h10a4 4 0 0 0 .6-7.96A6 6 0 0 0 6.1 6.6 4.5 4.5 0 0 0 7 15.5z"/><path d="M8.5 18.5l-1 2.5M12.5 18.5l-1 2.5M16.5 18.5l-1 2.5"/>',
    storm: '<path d="M7 15.5h10a4 4 0 0 0 .6-7.96A6 6 0 0 0 6.1 6.6 4.5 4.5 0 0 0 7 15.5z"/><path d="M12.5 13l-2.5 4h3l-2 4.5"/>',
    snow: '<path d="M7 15.5h10a4 4 0 0 0 .6-7.96A6 6 0 0 0 6.1 6.6 4.5 4.5 0 0 0 7 15.5z"/><path d="M8.5 19.2v.1M12 21v.1M15.5 19.2v.1M12 18v.1"/>',
    mist: '<path d="M4 8.5h13M7 12h13M4 15.5h10M17 15.5h3M9 19h8"/>',
    warn: '<path d="M12 4.5l8.5 14.7h-17z"/><path d="M12 10.5v4M12 17.3v.1"/>',
    offline: '<path d="M3 3l18 18M8.5 15.3a5 5 0 0 1 7 0M5.2 11.9a9.6 9.6 0 0 1 3.4-2.2M12.3 8.6a9.6 9.6 0 0 1 6.5 3.3M2.3 8.4A14 14 0 0 1 6 5.9M10 4.7a14 14 0 0 1 11.7 3.7M12 19v.1"/>',
    shield: '<path d="M12 3l7.5 2.8v5.4c0 4.6-3.1 8.2-7.5 9.8-4.4-1.6-7.5-5.2-7.5-9.8V5.8z"/><path d="M9 12l2.2 2.2L15.2 10"/>',
    heart: '<path d="M12 20s-7.5-4.5-7.5-10.2A4.3 4.3 0 0 1 12 7.2a4.3 4.3 0 0 1 7.5 2.6C19.5 15.5 12 20 12 20z"/><path d="M6.5 12.2h3l1.3-2.2 2 4.2 1.2-2h3.2"/>',
    ok: '<circle cx="12" cy="12" r="8.5"/><path d="M8.3 12.3l2.5 2.5 4.9-5.2"/>',
    cam: '<path d="M3 3l18 18M15.5 15.5V17a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 17V8.5A1.5 1.5 0 0 1 5 7h2M10.5 7H14a1.5 1.5 0 0 1 1.5 1.5v2.8l5-3v7.5"/>'
  };
  function icon(name) { return '<svg viewBox="0 0 24 24" aria-hidden="true">' + (P[name] || P.cloud) + "</svg>"; }

  // OpenWeatherMap icon code ("10d") -> our drawing
  function weatherIcon(code) {
    var n = String(code || "").slice(0, 2), night = String(code || "").slice(-1) === "n";
    switch (n) {
      case "01": return night ? "moon" : "sun";
      case "02": return night ? "cloudMoon" : "cloudSun";
      case "03": case "04": return "cloud";
      case "09": case "10": return "rain";
      case "11": return "storm";
      case "13": return "snow";
      case "50": return "mist";
      default: return "cloud";
    }
  }

  function linkDown(state) { return state && state._link === false; }

  // ============================================================ clock
  function Clock(el, opts) {
    setClass(el, "vs vs-clock");
    el.innerHTML = '<div class="vs-clock-time"><span></span><span class="vs-clock-side"><b></b><span></span></span></div><div class="vs-clock-date"></div>';
    var hm = el.querySelector(".vs-clock-time > span"), sec = el.querySelector(".vs-clock-side b"),
        ap = el.querySelector(".vs-clock-side span"), date = el.querySelector(".vs-clock-date");
    function tick() {
      var d = new Date(), h = d.getHours();
      var hmText = (opts.timeFormat === 24 ? pad(h) : String(h % 12 || 12)) + ":" + pad(d.getMinutes());
      if (hm.textContent !== hmText) { hm.textContent = hmText; }
      sec.textContent = opts.showSeconds === false ? "" : pad(d.getSeconds());
      var apText = opts.timeFormat === 24 ? "" : (h < 12 ? "AM" : "PM");
      if (ap.textContent !== apText) { ap.textContent = apText; }
      var dText = DAYS[d.getDay()] + ", " + d.getDate() + " " + MONTHS[d.getMonth()];
      if (date.textContent !== dText) { date.textContent = dText; }
    }
    tick();
    // Runs from the mirror's own clock, so it keeps working with no network and no hub (UC-1).
    var timer = setInterval(tick, 1000);
    return { update: function () {}, destroy: function () { clearInterval(timer); } };
  }

  // ============================================================ weather
  function staleLine(text) { return '<div class="vs-stale">' + icon("offline") + "<span>" + esc(text) + "</span></div>"; }

  function Weather(el, opts) {
    return {
      update: function (state) {
        var w = (state && state.weather) || {}, h24 = opts.timeFormat === 24, html, cls = "vs vs-weather";
        if (!w.ok) {
          html = w.configured === false
            ? '<div class="vs-setup"><strong>Weather is not set up</strong>Add <code>OPENWEATHER_API_KEY</code> to the .env file.</div>'
            : '<div class="vs-setup"><strong>' + (w.fetched_at === 0 && !w.error ? "Loading weather" : "Weather unavailable") + "</strong>" + esc(w.error || "") + "</div>";
        } else {
          var unit = w.units === "imperial" ? "°F" : "°";
          html = '<div class="vs-weather-now">' + icon(weatherIcon(w.icon)) +
            '<div class="vs-weather-temp">' + round(w.temp) + "°</div></div>" +
            '<div class="vs-weather-desc">' + esc(w.description) + (w.city ? ' <span class="vs-low">in ' + esc(w.city) + "</span>" : "") + "</div>" +
            '<div class="vs-weather-meta"><span>Humidity ' + round(w.humidity) + "%</span><span>Feels like " + round(w.feels_like) + unit + "</span></div>";
          if (w.forecast && w.forecast.length && opts.showForecast !== false) {
            html += '<div class="vs-forecast">';
            w.forecast.slice(0, 4).forEach(function (f) {
              html += "<div><small>" + fmtHour(new Date(f.time * 1000), h24) + "</small>" + icon(weatherIcon(f.icon)) + "<b>" + round(f.temp) + "°</b></div>";
            });
            html += "</div>";
          }
          if (w.stale || linkDown(state)) {
            cls += " is-stale";
            html += staleLine("Offline. Showing data from " + fmtTime(new Date(w.fetched_at * 1000), h24));
          }
        }
        setClass(el, cls);
        setHTML(el, html);
      }
    };
  }

  // ============================================================ calendar
  function Calendar(el, opts) {
    var last = null;
    function draw() {
      var state = last, c = (state && state.calendar) || {}, h24 = opts.timeFormat === 24;
      var now = new Date(), nowS = now.getTime() / 1000, html = "", cls = "vs vs-calendar";
      var events = (c.events || []).filter(function (e) { return (e.end || e.start) >= nowS; })
        .slice(0, opts.maxEvents || 5);

      if (c.configured === false) {
        html = '<div class="vs-setup"><strong>Calendar is not set up</strong>Add <code>GOOGLE_CALENDAR_ICS_URL</code> to the .env file.</div>';
      } else {
        if (c.error) { html += '<div class="vs-cal-msg">' + icon("offline") + "<span>" + esc(c.error) + "</span></div>"; }
        if (c.stale || linkDown(state)) { cls += " is-stale"; }
        if (!events.length) {
          if (!c.error) { html += '<div class="vs-setup"><strong>' + (c.ok ? "Nothing coming up" : "Loading calendar") + "</strong>" + (c.ok ? "Your calendar is clear." : "") + "</div>"; }
        } else {
          var lastDay = null;
          events.forEach(function (e, i) {
            var start = new Date(e.start * 1000), diff = dayDiff(start, now);
            var running = e.start <= nowS;
            var day = diff <= 0 ? "Today" : diff === 1 ? "Tomorrow" : DAYS[start.getDay()] + " " + start.getDate() + " " + MONTHS[start.getMonth()].slice(0, 3);
            if (day !== lastDay) { html += '<div class="vs-cal-day">' + day + "</div>"; lastDay = day; }
            var when;
            if (e.all_day) { when = "All day"; }
            else if (running) { when = "Now"; }
            else {
              var mins = Math.round((e.start - nowS) / 60);
              when = (mins < 60 && mins >= 0) ? "in " + Math.max(1, mins) + " min" : fmtTime(start, h24);
            }
            // UC-1: the next two events are the ones to read at a glance.
            html += '<div class="vs-cal-row' + (i < 2 ? " is-next" : "") + (running && !e.all_day ? " is-now" : "") + '">' +
              '<span class="vs-cal-when">' + when + '</span><span class="vs-cal-title">' + esc(e.title) +
              (e.location && i < 2 ? "<small>" + esc(e.location) + "</small>" : "") + "</span></div>";
          });
        }
        if (c.ok && (c.stale || linkDown(state)) && !c.error) {
          html += staleLine("Offline. Showing events from " + fmtTime(new Date(c.fetched_at * 1000), h24));
        }
      }
      setClass(el, cls);
      setHTML(el, html);
    }
    var timer = setInterval(draw, 20000);   // keeps "in 12 min" moving between updates
    return { update: function (state) { last = state; draw(); }, destroy: function () { clearInterval(timer); } };
  }

  // ============================================================ status
  function wifiWord(dbm) {
    if (dbm == null) { return null; }
    return dbm >= -60 ? "Strong" : dbm >= -70 ? "Good" : dbm >= -80 ? "Weak" : "Very weak";
  }
  function Status(el) {
    return {
      update: function (state) {
        state = state || {};
        var s = state.system || {}, ai = state.ai || {}, cam = state.camera || {}, rows = [];
        function row(label, value, kind) { rows.push("<dt>" + label + '</dt><dd class="' + (kind || "") + '"><i></i>' + esc(value) + "</dd>"); }

        if (linkDown(state)) { row("Mirror service", "Not running", "bad"); }

        if (ai.mode === "local") { row("Assistant", "On this mirror", "ice"); }
        else if (ai.mode === "limited") { row("Assistant", "Limited", "warn"); }
        else { row("Assistant", "Cloud", "ok"); }

        if (s.online === false) { row("Internet", "Offline", "warn"); }
        else if (s.wifi_dbm != null) { row("Wi-Fi", wifiWord(s.wifi_dbm), s.wifi_dbm >= -70 ? "ok" : "warn"); }
        else if (s.online === true) { row("Internet", "Connected", "ok"); }

        if (s.cpu_temp != null) {
          row("Temperature", round(s.cpu_temp) + "°C" + (s.throttled ? ", cooling down" : ""), s.throttled || s.cpu_temp >= 85 ? "bad" : s.cpu_temp >= 75 ? "warn" : "ok");
        }
        if (cam.reachable === false) { row("Camera", cam.error || "Not running", "warn"); }
        else if (cam.reachable && cam.camera_enabled !== false && cam.camera_online === false) { row("Camera", "Not detected", "warn"); }
        else if (cam.reachable) { row("Camera", cam.camera_enabled === false ? "Paused" : "On" + (cam.resolution ? ", " + cam.resolution : ""), cam.camera_enabled === false ? "" : "ok"); }

        setClass(el, "vs");
        setHTML(el, '<dl class="vs-status">' + rows.join("") + "</dl>");
      }
    };
  }

  // ============================================================ guard (modes)
  function Guard(el) {
    return {
      update: function (state) {
        var cam = (state && state.camera) || {}, html = "";
        if (cam.reachable && cam.security_mode) {
          html += '<div class="vs-guard-item is-on">' + '<div><b>Security mode is on</b><span>' +
            (cam.camera_enabled === false ? "Camera is paused, not watching" : "Watching for movement") + "</span></div>" + icon("shield") + "</div>";
        }
        if (cam.reachable && cam.home_alone_mode) {
          html += '<div class="vs-guard-item is-on">' + '<div><b>Home alone mode is on</b><span>' +
            (cam.camera_enabled === false ? "Camera is paused, not watching" : cam.fall_detector === "shape" ? "Watching for falls (basic mode)" : "Watching for falls") + "</span></div>" + icon("heart") + "</div>";
        }
        setClass(el, "vs vs-guard");
        setHTML(el, html);
      }
    };
  }

  // ============================================================ assistant
  var HINTS = { listening: "Listening", transcribing: "Got it", thinking: "Thinking", speaking: "", error: "" };

  function Assistant(el) {
    setClass(el, "vs vs-assistant");
    var bars = "", n = 41, i, mid = (n - 1) / 2;
    for (i = 0; i < n; i++) {
      // taller in the middle, with a fixed pseudo-random wobble so it looks like speech
      var env = Math.pow(Math.cos(((i - mid) / mid) * (Math.PI / 2)), 1.4);
      var amp = Math.max(0.1, env * (0.55 + 0.45 * Math.abs(Math.sin(i * 12.9898) * 43758.5453 % 1)));
      var delay = (Math.abs(Math.sin(i * 78.233) * 9631.77) % 1.1).toFixed(2);
      bars += '<i style="--a:' + amp.toFixed(2) + ";--d:" + delay + ";--i:" + i + '"></i>';
    }
    el.innerHTML = '<div class="vs-as-heard"></div><div class="vs-as-reply"></div><div class="vs-wave">' + bars + '</div><div class="vs-as-hint"></div>';
    var heard = el.querySelector(".vs-as-heard"), reply = el.querySelector(".vs-as-reply"), hint = el.querySelector(".vs-as-hint");

    return {
      update: function (state) {
        state = state || {};
        var v = state.voice || {}, ai = state.ai || {}, st = v.state || "idle";
        var wake = (state.hub && state.hub.wake_word) || "Hey Vision";
        var off = linkDown(state) || !v.connected;
        if (off || v.muted) { st = "idle"; }

        setClass(el, "vs vs-assistant s-" + st + (off ? " is-off" : ""));
        var h = st === "idle" ? "" : (v.heard ? "“" + v.heard + "”" : "");
        if (heard.textContent !== h) { heard.textContent = h; }
        var r = (st === "speaking" || st === "listening") ? (v.reply || "") : st === "error" ? (v.message || "") : "";
        if (reply.textContent !== r) { reply.textContent = r; }

        var hintHTML;
        if (off) { hintHTML = "Voice assistant is not running"; }
        else if (v.muted) { hintHTML = "Voice assistant is muted. Unmute it in the app."; }
        else if (st === "idle") { hintHTML = "Say <b>“" + esc(wake) + "”</b>"; }
        else if (st === "speaking" && ai.mode === "local") { hintHTML = "Answered on this mirror, no internet needed"; }
        else if (st === "error") { hintHTML = "Say <b>“" + esc(wake) + "”</b> to try again"; }
        else { hintHTML = HINTS[st] || ""; }
        setHTML(hint, hintHTML);
      }
    };
  }

  // ============================================================ alerts
  var TAKEOVER = {
    FALL_DETECTED: { kind: "danger", title: "Fall detected", text: "Your emergency contact is being alerted. Stay where you are." },
    MOTIONLESS_ESCALATION: { kind: "danger", title: "No response", text: "Your emergency contact is being alerted. Move if you are fine." },
    FALL_COUNTDOWN: { kind: "ask", title: "Are you okay?", text: "Move or stand up if you can hear this.", count: true, after: "Your emergency contact is alerted when this reaches zero." },
    MOTIONLESS_PROMPT: { kind: "ask", title: "Are you okay?", text: "You have not moved for a while. Move or wave if you can hear this.", count: true, after: "Your emergency contact is alerted when this reaches zero." },
    SECURITY_ALERT: { kind: "danger", title: "Person detected", text: "Security mode is on. The owner has been alerted and can see the live camera." }
  };
  var CIRC = 2 * Math.PI * 54;

  function Alerts(el, opts) {
    setClass(el, "vs vs-alerts");
    var last = null, timer = null, audio = null, beeper = null;

    function startAlarm() {
      if (beeper) { return; }
      try {
        audio = audio || new (root.AudioContext || root.webkitAudioContext)();
        var beep = function () {
          var o = audio.createOscillator(), g = audio.createGain();
          o.type = "square"; o.frequency.value = 880;
          g.gain.setValueAtTime(0.25, audio.currentTime);
          g.gain.exponentialRampToValueAtTime(0.001, audio.currentTime + 0.35);
          o.connect(g); g.connect(audio.destination); o.start(); o.stop(audio.currentTime + 0.36);
        };
        beep(); beeper = setInterval(beep, 700);
      } catch (e) { /* no audio device: the camera backend still speaks through espeak */ }
    }
    function stopAlarm() { if (beeper) { clearInterval(beeper); beeper = null; } }

    function draw() {
      var state = last || {}, alerts = state.alerts || [], now = Date.now() / 1000, html = "";
      var h24 = opts.timeFormat === 24;
      // Offset between the hub's clock and this screen's clock (same machine on the mirror).
      var skew = state.now ? (state.__recv || now) - state.now : 0;
      var top = null, i, a, banner = null;
      for (i = 0; i < alerts.length; i++) {
        a = alerts[i];
        if (TAKEOVER[a.type]) {
          if (TAKEOVER[a.type].count && a.deadline != null && a.deadline + skew - now < -1) { continue; }
          if (!top) { top = a; }
        } else if (a.type === "CAMERA_OFFLINE" && !banner) {
          banner = '<div class="vs-banner k-warn">' + icon("cam") + "<span>Camera offline. Monitoring is interrupted.</span></div>";
        } else if (a.type === "FALL_RECOVERED" && !banner && !(a.until != null && a.until + skew < now)) {
          banner = '<div class="vs-banner k-good">' + icon("ok") + "<span>Brief fall detected. Person recovered.</span></div>";
        }
      }
      var emergency = false;
      if (top) {
        var t = TAKEOVER[top.type];
        emergency = top.type === "FALL_DETECTED" || top.type === "MOTIONLESS_ESCALATION";
        html += '<div class="vs-takeover k-' + t.kind + '"><h2>' + t.title + "</h2><p>" + t.text + "</p>";
        if (t.count && top.deadline != null) {
          var win = top.window || 30, left = Math.max(0, top.deadline + skew - now);
          html += '<div class="vs-count"><svg viewBox="0 0 120 120"><circle cx="60" cy="60" r="54"/><circle cx="60" cy="60" r="54" stroke-dasharray="' +
            CIRC.toFixed(1) + '" stroke-dashoffset="' + (CIRC * (1 - Math.min(1, left / win))).toFixed(1) + '"/></svg><b>' + Math.ceil(left) + "</b></div>";
        }
        html += "<small>" + (t.after || ("Since " + fmtTime(new Date((top.timestamp + skew) * 1000), h24))) +
          (emergency && state.system && state.system.online === false ? ". Internet is down: the phone will call over the mobile network." : "") + "</small></div>";
      }
      if (banner && !top) { html += banner; }
      setHTML(el, html);

      // UC-3 A4: with no internet, sound a local alarm at once. "always" sounds it every time.
      var want = emergency && (opts.alarmSound === "always" || (opts.alarmSound !== "never" && state.system && state.system.online === false));
      if (want) { startAlarm(); } else { stopAlarm(); }

      var needTick = !!top && TAKEOVER[top.type].count || /k-good/.test(html);
      if (needTick && !timer) { timer = setInterval(draw, 250); }
      if (!needTick && timer) { clearInterval(timer); timer = null; }
    }
    return {
      update: function (state) { last = state; if (state && !state.__recv) { state.__recv = Date.now() / 1000; } draw(); },
      destroy: function () { stopAlarm(); if (timer) { clearInterval(timer); } }
    };
  }


  // ============================================================ ambient background
  // Soft colour clouds that drift slowly behind the widgets. On a two-way mirror
  // every lit pixel hides the reflection, so there are three levels:
  //   "off" = pure black (best mirror), "subtle" = faint glow, "vivid" = showpiece.
  // The glow also reacts: it turns ice-blue and rises while the assistant listens
  // or speaks, and switches off during an emergency so the alert is unmistakable.
  var URGENT = { FALL_DETECTED: 1, MOTIONLESS_ESCALATION: 1, FALL_COUNTDOWN: 1, MOTIONLESS_PROMPT: 1, SECURITY_ALERT: 1 };
  function Ambient(el, opts) {
    el.innerHTML = '<i class="b1"></i><i class="b2"></i><i class="b3"></i><i class="b4"></i><i class="voice"></i>';
    return {
      update: function (state) {
        state = state || {};
        var level = opts.ambient || (state.hub && state.hub.ambient) || "subtle";
        if (["off", "subtle", "vivid"].indexOf(level) < 0) { level = "subtle"; }
        var v = state.voice || {}, st = (v.connected && !v.muted && v.state) || "idle";
        var urgent = (state.alerts || []).some(function (a) { return URGENT[a.type]; });
        setClass(el, "vs-ambient lvl-" + (urgent ? "off" : level) + " s-" + st +
          (state.system && state.system.online === false ? " is-offline" : ""));
      }
    };
  }


  // ============================================================ Wi-Fi setup
  // Full screen while the mirror has no network and is waiting to be set up from a
  // phone (wifi/visionsync_wifi.py). Emergency alerts still appear on top of it.
  function Setup(el) {
    setClass(el, "vs vs-setup-wrap");
    return {
      update: function (state) {
        var s = (state && state.setup) || {}, html = "";
        if (s.active) {
          var st = s.state || "waiting";
          if (st === "connecting") {
            html = '<div class="vs-wifi s-connecting"><h2>Connecting to ' + esc(s.network) + '</h2>' +
              '<p>This takes up to half a minute.</p><div class="vs-wifi-dots"><i></i><i></i><i></i></div></div>';
          } else if (st === "connected") {
            html = '<div class="vs-wifi s-connected">' + icon("ok") + '<h2>Connected to ' + esc(s.network) + '</h2>' +
              '<p>Your mirror is online.</p></div>';
          } else if (st === "checking") {
            html = '<div class="vs-wifi s-connecting"><h2>Looking for your Wi-Fi</h2><p>Checking the networks this mirror knows.</p></div>';
          } else if (st === "error") {
            html = '<div class="vs-wifi s-error">' + icon("warn") + '<h2>Wi-Fi setup is not available</h2><p>' + esc(s.message) + '</p></div>';
          } else {
            var qr = root.VSQR ? root.VSQR.svg(root.VSQR.wifiText(s.ssid, s.password)) : "";
            html = '<div class="vs-wifi s-waiting">' +
              '<div class="vs-wifi-qr">' + qr + '</div>' +
              '<div class="vs-wifi-text"><h2>Connect your mirror to Wi-Fi</h2>' +
              (s.message ? '<div class="vs-wifi-msg">' + icon("warn") + '<span>' + esc(s.message) + '</span></div>' : "") +
              '<ol><li>Scan this code with your phone camera to join <b>' + esc(s.ssid) + '</b>.</li>' +
              '<li>A setup page opens. Choose your home Wi-Fi and enter its password.</li>' +
              '<li>The mirror connects and shows your dashboard.</li></ol>' +
              '<p class="vs-wifi-manual">No camera? Join the Wi-Fi <b>' + esc(s.ssid) + '</b> with password <b>' + esc(s.password) +
              '</b>, then open <b>' + esc((s.url || "").replace(/^https?:\/\//, "").replace(/\/$/, "")) + '</b> in the browser.</p></div></div>';
          }
        }
        setHTML(el, html);
      }
    };
  }

  // ------------------------------------------------------------ public API
  // ------------------------------------------------------------ agenda: tasks and reminders
  // Sent by the voice assistant ("add buy milk to my tasks", "remind me at 9 pm to ...").
  function Agenda(el, opts) {
    function esc(s) {
      return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
      });
    }
    function clock(d) {
      var h = d.getHours(), m = ("0" + d.getMinutes()).slice(-2);
      if (opts && opts.timeFormat === 24) { return h + ":" + m; }
      return ((h % 12) || 12) + ":" + m + (h < 12 ? " AM" : " PM");
    }
    function when(iso) {
      var d = new Date(iso);
      if (isNaN(d.getTime())) { return ""; }
      var today = new Date(); today.setHours(0, 0, 0, 0);
      var days = Math.round((new Date(d.getFullYear(), d.getMonth(), d.getDate()) - today) / 86400000);
      var day = days === 0 ? "" : days === 1 ? "Tomorrow " : ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getDay()] + " ";
      return day + clock(d);
    }
    return {
      update: function (state) {
        var a = (state && state.agenda) || {}, tasks = a.tasks || [], rem = a.reminders || [], html = "";
        if (rem.length) {
          html += '<div class="vs-ag-head">Reminders</div>';
          rem.slice(0, 3).forEach(function (r) {
            html += '<div class="vs-ag-row"><span class="vs-ag-text">' + esc(r.title) +
                    '</span><span class="vs-ag-when">' + esc(when(r.due_at)) + "</span></div>";
          });
        }
        if (tasks.length) {
          html += '<div class="vs-ag-head">To do</div>';
          tasks.slice(0, 4).forEach(function (t) {
            html += '<div class="vs-ag-row"><span class="vs-ag-text">' + esc(t) + "</span><i></i></div>";
          });
          if (tasks.length > 4) { html += '<div class="vs-ag-more">and ' + (tasks.length - 4) + " more</div>"; }
        }
        el.className = "vs vs-agenda";
        if (el.innerHTML !== html) { el.innerHTML = html; }
      }
    };
  }

  var TYPES = { clock: Clock, weather: Weather, calendar: Calendar, status: Status, guard: Guard, assistant: Assistant, alerts: Alerts, ambient: Ambient, setup: Setup, agenda: Agenda };

  root.VSWidgets = {
    types: Object.keys(TYPES),
    mount: function (type, el, opts) {
      var make = TYPES[type];
      if (!make) { el.textContent = "Unknown VisionSync widget: " + type; return { update: function () {} }; }
      var w = make(el, opts || {});
      if (!w.destroy) { w.destroy = function () {}; }
      return w;
    },
    /* Adds/removes the "throttled" class that stops all animations (NFR-S1). */
    applyThrottle: function (state) {
      var on = !!(state && state.system && state.system.throttled);
      document.documentElement.classList.toggle("vs-throttled", on);
    }
  };
})(typeof window !== "undefined" ? window : this);
