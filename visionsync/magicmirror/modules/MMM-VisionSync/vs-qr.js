/*
 * vs-qr.js - Small QR code generator (byte mode, error correction level M,
 * versions 1 to 10). Enough for a Wi-Fi join code or a short web address.
 * Follows the QR specification (ISO/IEC 18004), written after Nayuki's
 * public-domain reference design. No network, no libraries.
 *
 *   VSQR.svg("WIFI:T:WPA;S:VisionSync-Setup;P:12345678;;")  -> "<svg ...>"
 *   VSQR.matrix(text) -> array of rows of true/false (true = dark)
 */
(function (root) {
  "use strict";

  // Level M, by version (index 0 unused)
  var ECC_PER_BLOCK = [0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26];
  var BLOCKS = [0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5];
  var FORMAT_M = 0;            // format-info code for level M

  function rawModules(v) {
    var r = (16 * v + 128) * v + 64;
    if (v >= 2) {
      var n = Math.floor(v / 7) + 2;
      r -= (25 * n - 10) * n - 55;
      if (v >= 7) { r -= 36; }
    }
    return r;
  }
  function dataCodewords(v) { return Math.floor(rawModules(v) / 8) - ECC_PER_BLOCK[v] * BLOCKS[v]; }

  // ---- Reed-Solomon over GF(256), polynomial 0x11D
  function mul(x, y) {
    var z = 0;
    for (var i = 7; i >= 0; i--) {
      z = (z << 1) ^ ((z >>> 7) * 0x11D);
      z ^= ((y >>> i) & 1) * x;
    }
    return z & 0xFF;
  }
  function divisor(degree) {
    var r = [], i, j, root = 1;
    for (i = 0; i < degree - 1; i++) { r.push(0); }
    r.push(1);
    for (i = 0; i < degree; i++) {
      for (j = 0; j < r.length; j++) {
        r[j] = mul(r[j], root);
        if (j + 1 < r.length) { r[j] ^= r[j + 1]; }
      }
      root = mul(root, 0x02);
    }
    return r;
  }
  function remainder(data, div) {
    var r = div.map(function () { return 0; });
    data.forEach(function (b) {
      var factor = b ^ r.shift();
      r.push(0);
      div.forEach(function (c, i) { r[i] ^= mul(c, factor); });
    });
    return r;
  }

  function utf8(text) {
    var s = unescape(encodeURIComponent(text)), out = [];
    for (var i = 0; i < s.length; i++) { out.push(s.charCodeAt(i)); }
    return out;
  }

  function alignmentPositions(v, size) {
    if (v === 1) { return []; }
    var n = Math.floor(v / 7) + 2;
    var step = Math.ceil((v * 4 + 4) / (n * 2 - 2)) * 2;
    var out = [6];
    for (var i = 0, pos = size - 7; i < n - 1; i++, pos -= step) { out.splice(1, 0, pos); }
    return out;
  }

  function matrix(text) {
    var bytes = utf8(text), v, i;
    for (v = 1; v <= 10; v++) {
      if (4 + (v <= 9 ? 8 : 16) + 8 * bytes.length <= dataCodewords(v) * 8) { break; }
    }
    if (v > 10) { return null; }

    // ---- data bits
    var bits = [];
    function put(val, len) { for (var k = len - 1; k >= 0; k--) { bits.push((val >>> k) & 1); } }
    put(4, 4);                                  // byte mode
    put(bytes.length, v <= 9 ? 8 : 16);
    bytes.forEach(function (b) { put(b, 8); });
    var capacity = dataCodewords(v) * 8;
    put(0, Math.min(4, capacity - bits.length));
    put(0, (8 - bits.length % 8) % 8);
    for (var pad = 0xEC; bits.length < capacity; pad ^= 0xEC ^ 0x11) { put(pad, 8); }
    var data = [];
    for (i = 0; i < bits.length; i += 8) {
      var b = 0;
      for (var k = 0; k < 8; k++) { b = (b << 1) | bits[i + k]; }
      data.push(b);
    }

    // ---- error correction and interleaving
    var nBlocks = BLOCKS[v], eccLen = ECC_PER_BLOCK[v], raw = Math.floor(rawModules(v) / 8);
    var nShort = nBlocks - raw % nBlocks, shortLen = Math.floor(raw / nBlocks);
    var div = divisor(eccLen), blocks = [], at = 0;
    for (i = 0; i < nBlocks; i++) {
      var dat = data.slice(at, at + shortLen - eccLen + (i < nShort ? 0 : 1));
      at += dat.length;
      var ecc = remainder(dat, div);
      if (i < nShort) { dat.push(0); }
      blocks.push(dat.concat(ecc));
    }
    var codewords = [];
    for (i = 0; i < blocks[0].length; i++) {
      for (var j = 0; j < blocks.length; j++) {
        if (i !== shortLen - eccLen || j >= nShort) { codewords.push(blocks[j][i]); }
      }
    }

    // ---- grid
    var size = v * 4 + 17, m = [], fn = [];
    for (i = 0; i < size; i++) { m.push(new Array(size).fill(false)); fn.push(new Array(size).fill(false)); }
    function set(x, y, dark) { m[y][x] = dark; fn[y][x] = true; }

    for (i = 0; i < size; i++) { set(6, i, i % 2 === 0); set(i, 6, i % 2 === 0); }
    [[3, 3], [size - 4, 3], [3, size - 4]].forEach(function (c) {
      for (var dy = -4; dy <= 4; dy++) {
        for (var dx = -4; dx <= 4; dx++) {
          var d = Math.max(Math.abs(dx), Math.abs(dy)), x = c[0] + dx, y = c[1] + dy;
          if (x >= 0 && x < size && y >= 0 && y < size) { set(x, y, d !== 2 && d !== 4); }
        }
      }
    });
    var al = alignmentPositions(v, size), n = al.length;
    for (i = 0; i < n; i++) {
      for (j = 0; j < n; j++) {
        if ((i === 0 && j === 0) || (i === 0 && j === n - 1) || (i === n - 1 && j === 0)) { continue; }
        for (var ay = -2; ay <= 2; ay++) {
          for (var ax = -2; ax <= 2; ax++) { set(al[i] + ax, al[j] + ay, Math.max(Math.abs(ax), Math.abs(ay)) !== 1); }
        }
      }
    }
    function formatBits(mask) {
      var d = (FORMAT_M << 3) | mask, rem = d;
      for (var q = 0; q < 10; q++) { rem = (rem << 1) ^ ((rem >>> 9) * 0x537); }
      var f = ((d << 10) | rem) ^ 0x5412;
      function bit(t) { return ((f >>> t) & 1) !== 0; }
      for (q = 0; q <= 5; q++) { set(8, q, bit(q)); }
      set(8, 7, bit(6)); set(8, 8, bit(7)); set(7, 8, bit(8));
      for (q = 9; q < 15; q++) { set(14 - q, 8, bit(q)); }
      for (q = 0; q < 8; q++) { set(size - 1 - q, 8, bit(q)); }
      for (q = 8; q < 15; q++) { set(8, size - 15 + q, bit(q)); }
      set(8, size - 8, true);
    }
    formatBits(0);
    if (v >= 7) {
      var rem = v;
      for (i = 0; i < 12; i++) { rem = (rem << 1) ^ ((rem >>> 11) * 0x1F25); }
      var vb = (v << 12) | rem;
      for (i = 0; i < 18; i++) {
        var bt = ((vb >>> i) & 1) !== 0, a = size - 11 + i % 3, c2 = Math.floor(i / 3);
        set(a, c2, bt); set(c2, a, bt);
      }
    }

    // ---- place data, zigzag from the bottom right
    var idx = 0;
    for (var right = size - 1; right >= 1; right -= 2) {
      if (right === 6) { right = 5; }
      for (var vert = 0; vert < size; vert++) {
        for (var jj = 0; jj < 2; jj++) {
          var x = right - jj, upward = ((right + 1) & 2) === 0, y = upward ? size - 1 - vert : vert;
          if (!fn[y][x] && idx < codewords.length * 8) {
            m[y][x] = ((codewords[idx >>> 3] >>> (7 - (idx & 7))) & 1) !== 0;
            idx++;
          }
        }
      }
    }

    // ---- choose the mask with the lowest penalty
    function maskHit(mask, x, y) {
      switch (mask) {
        case 0: return (x + y) % 2 === 0;
        case 1: return y % 2 === 0;
        case 2: return x % 3 === 0;
        case 3: return (x + y) % 3 === 0;
        case 4: return (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0;
        case 5: return x * y % 2 + x * y % 3 === 0;
        case 6: return (x * y % 2 + x * y % 3) % 2 === 0;
        default: return ((x + y) % 2 + x * y % 3) % 2 === 0;
      }
    }
    function applyMask(mask) {
      for (var yy = 0; yy < size; yy++) {
        for (var xx = 0; xx < size; xx++) {
          if (!fn[yy][xx] && maskHit(mask, xx, yy)) { m[yy][xx] = !m[yy][xx]; }
        }
      }
    }
    function penalty() {
      var p = 0, dark = 0, yy, xx;
      for (var pass = 0; pass < 2; pass++) {           // long runs in rows, then columns
        for (yy = 0; yy < size; yy++) {
          var run = 1;
          for (xx = 1; xx <= size; xx++) {
            var cur = xx < size && (pass ? m[xx][yy] : m[yy][xx]) === (pass ? m[xx - 1][yy] : m[yy][xx - 1]);
            if (cur) { run++; } else { if (run >= 5) { p += run - 2; } run = 1; }
          }
        }
      }
      for (yy = 0; yy < size - 1; yy++) {               // 2x2 blocks of one colour
        for (xx = 0; xx < size - 1; xx++) {
          var c0 = m[yy][xx];
          if (c0 === m[yy][xx + 1] && c0 === m[yy + 1][xx] && c0 === m[yy + 1][xx + 1]) { p += 3; }
        }
      }
      for (yy = 0; yy < size; yy++) { for (xx = 0; xx < size; xx++) { if (m[yy][xx]) { dark++; } } }
      var total = size * size;
      p += (Math.ceil(Math.abs(dark * 20 - total * 10) / total) - 1) * 10;
      return p;
    }
    var best = 0, bestScore = Infinity;
    for (var mk = 0; mk < 8; mk++) {
      applyMask(mk); formatBits(mk);
      var sc = penalty();
      if (sc < bestScore) { bestScore = sc; best = mk; }
      applyMask(mk);
    }
    applyMask(best); formatBits(best);
    return m;
  }

  /* Dark modules on white with the 4-module quiet zone phones need. */
  function svg(text) {
    var m = matrix(text);
    if (!m) { return ""; }
    var size = m.length, q = 4, d = "";
    for (var y = 0; y < size; y++) {
      for (var x = 0; x < size; x++) { if (m[y][x]) { d += "M" + (x + q) + " " + (y + q) + "h1v1h-1z"; } }
    }
    var w = size + q * 2;
    return '<svg class="vs-qr" viewBox="0 0 ' + w + " " + w + '" shape-rendering="crispEdges" role="img" aria-label="QR code">' +
      '<rect width="' + w + '" height="' + w + '" fill="#fff"/><path d="' + d + '" fill="#000"/></svg>';
  }

  /* Text for a "join this Wi-Fi" QR code (phones' camera apps understand it). */
  function wifiText(ssid, password) {
    function e(s) { return String(s).replace(/([\\;,:"])/g, "\\$1"); }
    return "WIFI:T:" + (password ? "WPA" : "nopass") + ";S:" + e(ssid) + ";" + (password ? "P:" + e(password) + ";" : "") + ";";
  }

  root.VSQR = { matrix: matrix, svg: svg, wifiText: wifiText };
  if (typeof module !== "undefined") { module.exports = root.VSQR; }
})(typeof window !== "undefined" ? window : this);
