/*
 * node_helper.js - runs inside MagicMirror's Node.js process.
 * Keeps one connection to the VisionSync hub's Unix socket and passes every
 * state update to the widgets. Reconnects by itself if the hub restarts
 * (NFR-Q2: either side can be restarted without touching the other).
 */
const NodeHelper = require("node_helper");
const net = require("net");

module.exports = NodeHelper.create({
  start: function () {
    this.socketPath = null;
    this.client = null;
    this.lastState = null;
    this.connected = false;
    this.retryTimer = null;
    this.watchdog = null;
    console.log("[MMM-VisionSync] helper started");
  },

  stop: function () {
    clearTimeout(this.retryTimer);
    clearInterval(this.watchdog);
    if (this.client) { this.client.destroy(); }
  },

  socketNotificationReceived: function (notification, payload) {
    if (notification !== "VS_CONNECT") { return; }
    const path = (payload && payload.socketPath) || "/tmp/visionsync.sock";
    if (this.socketPath === null) {
      this.socketPath = path;
      this.connect();
    } else {
      // A browser (re)loaded: give it what we already have straight away.
      if (this.lastState) { this.sendSocketNotification("VS_STATE", this.lastState); }
      if (!this.connected) { this.sendSocketNotification("VS_LINK", { connected: false }); }
    }
  },

  connect: function () {
    clearTimeout(this.retryTimer);
    let buffer = "";
    let lastData = Date.now();
    const client = net.createConnection(this.socketPath);
    this.client = client;
    client.setEncoding("utf8");

    client.on("connect", () => {
      this.connected = true;
      console.log("[MMM-VisionSync] connected to hub at " + this.socketPath);
      client.write(JSON.stringify({ type: "hello", role: "dashboard", subscribe: true }) + "\n");
    });

    client.on("data", (chunk) => {
      lastData = Date.now();
      buffer += chunk;
      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl);
        buffer = buffer.slice(nl + 1);
        if (!line.trim()) { continue; }
        try {
          const msg = JSON.parse(line);
          if (msg.type === "state" && msg.state) {
            this.lastState = msg.state;
            this.sendSocketNotification("VS_STATE", msg.state);
          }
        } catch (e) {
          console.error("[MMM-VisionSync] bad message from hub: " + e.message);
        }
      }
      if (buffer.length > 2000000) { buffer = ""; }
    });

    const lost = (why) => {
      if (this.client !== client) { return; }
      this.client = null;
      client.destroy();
      clearInterval(this.watchdog);
      if (this.connected) { console.log("[MMM-VisionSync] hub connection lost (" + why + "), retrying"); }
      this.connected = false;
      this.sendSocketNotification("VS_LINK", { connected: false });
      this.retryTimer = setTimeout(() => this.connect(), 2000);
    };
    client.on("error", (e) => lost(e.code || e.message));
    client.on("close", () => lost("closed"));

    // The hub pings every 10 s. 30 s of silence means it is frozen: reconnect.
    clearInterval(this.watchdog);
    this.watchdog = setInterval(() => {
      if (Date.now() - lastData > 30000) { lost("no data for 30 s"); }
    }, 5000);
  }
});
