"""
system.py - Jetson health: temperature, CPU, memory, Wi-Fi, internet.

NFR-S1: above the limit (85 C) the hub turns on "throttle": the dashboard stops
its animations and the camera stream drops to 480p. It turns off again once the
board is back under the resume temperature, so it does not flip on and off.
NFR-P4: the internet check runs every 0.5 s with a 1 s timeout, so a drop is
known within about 1.5 seconds and the AI mode switches to "local".
"""

import glob
import logging
import os
import socket
import time

log = logging.getLogger("hub.system")


def read_cpu_temp(path=""):
    """Hottest thermal zone in Celsius, or None on a machine without sensors."""
    paths = [path] if path else (glob.glob("/sys/devices/virtual/thermal/thermal_zone*/temp")
                                  or glob.glob("/sys/class/thermal/thermal_zone*/temp"))
    best = None
    for p in paths:
        try:
            with open(p, "r") as f:
                v = float(f.read().strip())
        except (IOError, OSError, ValueError):
            continue
        if v > 1000:
            v /= 1000.0
        if v <= 0 or v > 150:       # Jetson's PMIC zone reports a fixed 100 C: ignore odd values
            continue
        if path == "" and abs(v - 100.0) < 0.01:
            continue
        best = v if best is None else max(best, v)
    return round(best, 1) if best is not None else None


class CpuMeter(object):
    """CPU use in percent between two calls (from /proc/stat)."""

    def __init__(self):
        self._last = self._read()

    @staticmethod
    def _read():
        try:
            with open("/proc/stat", "r") as f:
                parts = f.readline().split()[1:]
            vals = [float(x) for x in parts[:8]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            return sum(vals), idle
        except (IOError, OSError, ValueError, IndexError):
            return None

    def percent(self):
        cur = self._read()
        last, self._last = self._last, cur
        if not cur or not last or cur[0] == last[0]:
            return None
        busy = 1.0 - (cur[1] - last[1]) / (cur[0] - last[0])
        return round(max(0.0, min(1.0, busy)) * 100, 0)


def read_ram_percent():
    try:
        info = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = float(v.split()[0])
        total = info["MemTotal"]
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        return round((1 - avail / total) * 100, 0)
    except (IOError, OSError, ValueError, KeyError, ZeroDivisionError):
        return None


def read_wifi_dbm():
    try:
        with open("/proc/net/wireless", "r") as f:
            for line in f.readlines()[2:]:
                parts = line.split()
                if len(parts) >= 4:
                    return int(float(parts[3].rstrip(".")))
    except (IOError, OSError, ValueError):
        pass
    return None


def internet_up(hosts, timeout=1.0):
    """True when any of the hosts accepts a TCP connection. The hosts are tried
    at the same time, so the answer always comes within `timeout` seconds."""
    import threading
    result = {"up": False}
    done = threading.Event()

    def probe(hp):
        host, _, port = hp.partition(":")
        try:
            s = socket.create_connection((host, int(port or 53)), timeout=timeout)
            s.close()
            result["up"] = True
            done.set()
        except (OSError, ValueError):
            pass

    threads = []
    for hp in hosts:
        t = threading.Thread(target=probe, args=(hp,))
        t.daemon = True
        t.start()
        threads.append(t)
    end = time.time() + timeout + 0.1
    for t in threads:
        if done.is_set():
            break
        t.join(max(0.0, end - time.time()))
    return result["up"]


class ThermalGuard(object):
    """Turns throttle on at `limit`, off at `resume` (hysteresis)."""

    def __init__(self, limit=85.0, resume=75.0):
        self.limit = limit
        self.resume = min(resume, limit)
        self.throttled = False

    def feed(self, temp):
        """Returns 'on', 'off' or None when nothing changes."""
        if temp is None:
            return None
        if not self.throttled and temp >= self.limit:
            self.throttled = True
            log.error("Temperature %.1f C is over the limit (%.0f C): throttling.", temp, self.limit)
            return "on"
        if self.throttled and temp <= self.resume:
            self.throttled = False
            log.warning("Temperature back to %.1f C: throttle off.", temp)
            return "off"
        return None
