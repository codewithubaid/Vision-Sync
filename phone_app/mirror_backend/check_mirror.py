#!/usr/bin/env python3
"""Checks a Glass Connect mirror the same way the phone app talks to it.

Run it from ANY device on the tailnet (for example a laptop on mobile data):

    MIRROR_SECRET=your-secret python3 check_mirror.py 100.101.102.103 [port]

Needs only the Python standard library. Exit code 0 = every check passed.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

host = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MIRROR_HOST", "")
port = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("MIRROR_PORT", "8080")
secret = os.environ.get("MIRROR_SECRET", "")
if not host or not secret:
    sys.exit("Usage: MIRROR_SECRET=... python3 check_mirror.py <tailscale-ip-or-name> [port]")

base = f"http://{host}:{port}"
fails = 0


def report(name, ok, detail=""):
    global fails
    fails += 0 if ok else 1
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def call(path, method="GET", body=None, secret_header=secret):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("X-Mirror-Secret", secret_header)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=8) as r:
        raw = r.read().decode() or "{}"
        return r.status, json.loads(raw)


def check_json(name, path):
    try:
        t = time.time()
        status, j = call(path)
        report(name, status == 200, f"{(time.time() - t) * 1000:.0f} ms")
        return j
    except urllib.error.HTTPError as e:
        report(name, False, f"HTTP {e.code}")
    except Exception as e:  # noqa: BLE001
        report(name, False, f"{type(e).__name__}: {e}")
    return None


status = check_json("GET /status", "/status")
if status is not None:
    print("       camera_enabled=%s security=%s home_alone=%s" % (
        status.get("camera_enabled"), status.get("security_mode"), status.get("home_alone_mode")))
check_json("GET /alerts", "/alerts")
check_json("GET /events", "/events?limit=5")

# A wrong secret must be rejected (proves authentication is still on).
try:
    call("/status", secret_header="definitely-wrong")
    report("wrong secret is rejected", False, "server accepted a wrong secret!")
except urllib.error.HTTPError as e:
    report("wrong secret is rejected", e.code == 401, f"HTTP {e.code}")
except Exception as e:  # noqa: BLE001
    report("wrong secret is rejected", False, str(e))

# Camera control: set resolution to what it already is (changes nothing).
if status is not None:
    try:
        s, _ = call("/settings/resolution", "POST", {"preset": status.get("resolution", "720p")})
        report("POST /settings/resolution (no change)", s == 200)
    except Exception as e:  # noqa: BLE001
        report("POST /settings/resolution (no change)", False, str(e))

# MJPEG stream: read until two complete JPEG frames arrived.
try:
    url = f"{base}/stream.mjpg?token={urllib.parse.quote(secret)}"
    t = time.time()
    frames, buf = 0, b""
    with urllib.request.urlopen(url, timeout=10) as r:
        while frames < 2 and time.time() - t < 15:
            chunk = r.read(8192)
            if not chunk:
                break
            buf += chunk
            while True:
                a = buf.find(b"\xff\xd8")
                b = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
                if a < 0 or b < 0:
                    break
                frames += 1
                buf = buf[b + 2:]
    report("MJPEG stream (2 frames)", frames >= 2, f"{frames} frames in {time.time() - t:.1f}s")
except Exception as e:  # noqa: BLE001
    report("MJPEG stream (2 frames)", False, f"{type(e).__name__}: {e}")

print("\nALL CHECKS PASSED" if fails == 0 else f"\n{fails} CHECK(S) FAILED")
sys.exit(1 if fails else 0)
