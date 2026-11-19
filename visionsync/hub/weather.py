"""
weather.py - OpenWeatherMap (free tier) with a disk cache.

UC-1: when the network is down the dashboard keeps showing the last good data,
marked as stale. Only weather is cached on disk, never anything personal.
"""

import json
import logging
import os
import time

try:
    from urllib.request import urlopen, Request
    from urllib.parse import urlencode
    from urllib.error import HTTPError
except ImportError:  # pragma: no cover
    raise

log = logging.getLogger("hub.weather")

API = "https://api.openweathermap.org/data/2.5/"


def _get_json(url, timeout=8):
    req = Request(url, headers={"User-Agent": "VisionSync/1.0"})
    resp = urlopen(req, timeout=timeout)
    try:
        return json.loads(resp.read().decode("utf-8"))
    finally:
        resp.close()


def shape_current(j):
    """OpenWeatherMap 'current weather' reply -> the fields the widget shows."""
    w = (j.get("weather") or [{}])[0]
    main = j.get("main") or {}
    return {
        "city": j.get("name") or "",
        "temp": main.get("temp"),
        "feels_like": main.get("feels_like"),
        "humidity": main.get("humidity"),
        "temp_min": main.get("temp_min"),
        "temp_max": main.get("temp_max"),
        "description": (w.get("description") or "").capitalize(),
        "condition": w.get("main") or "",
        "icon": w.get("icon") or "",
        "wind": (j.get("wind") or {}).get("speed"),
        "sunrise": (j.get("sys") or {}).get("sunrise"),
        "sunset": (j.get("sys") or {}).get("sunset"),
        "observed_at": j.get("dt"),
    }


def shape_forecast(j, steps=4):
    """3-hourly forecast reply -> the next few steps."""
    out = []
    for item in (j.get("list") or [])[:steps]:
        w = (item.get("weather") or [{}])[0]
        out.append({
            "time": item.get("dt"),
            "temp": (item.get("main") or {}).get("temp"),
            "icon": w.get("icon") or "",
            "pop": item.get("pop"),
        })
    return out


class WeatherService(object):
    def __init__(self, cfg, fetch=_get_json):
        self.cfg = cfg
        self._fetch = fetch
        self.cache_path = os.path.join(cfg.cache_dir, "weather.json")
        self.data = None          # last good data
        self.fetched_at = 0
        self.last_error = ""
        self._load_cache()

    def configured(self):
        k = self.cfg.openweather_key
        return bool(k) and "your_" not in k

    def _load_cache(self):
        try:
            with open(self.cache_path, "r") as f:
                c = json.load(f)
            if c.get("place") == self._place():
                self.data = c.get("data")
                self.fetched_at = c.get("fetched_at", 0)
        except (IOError, OSError, ValueError):
            pass

    def _save_cache(self):
        try:
            if not os.path.isdir(self.cfg.cache_dir):
                os.makedirs(self.cfg.cache_dir)
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"place": self._place(), "fetched_at": self.fetched_at, "data": self.data}, f)
            os.rename(tmp, self.cache_path)
        except (IOError, OSError) as e:
            log.warning("Could not save the weather cache: %s", e)

    def _place(self):
        return "%s,%s|%s" % (self.cfg.city, self.cfg.country, self.cfg.units)

    def refresh(self):
        """Fetches now. Returns the section for the state store."""
        if not self.configured():
            self.last_error = "Add OPENWEATHER_API_KEY to .env"
            return self.section()
        q = {"q": "%s,%s" % (self.cfg.city, self.cfg.country) if self.cfg.country else self.cfg.city,
             "units": self.cfg.units, "appid": self.cfg.openweather_key}
        try:
            cur = shape_current(self._fetch(API + "weather?" + urlencode(q)))
            try:
                cur["forecast"] = shape_forecast(self._fetch(API + "forecast?" + urlencode(dict(q, cnt=4))))
            except Exception as e:  # the forecast is a nice-to-have
                log.info("Forecast not available: %s", e)
                cur["forecast"] = (self.data or {}).get("forecast", [])
            self.data = cur
            self.fetched_at = int(time.time())
            self.last_error = ""
            self._save_cache()
        except HTTPError as e:
            if e.code == 401:
                self.last_error = "OpenWeatherMap rejected the API key"
            elif e.code == 404:
                self.last_error = "City not found: %s" % self.cfg.city
            elif e.code == 429:
                self.last_error = "OpenWeatherMap free limit reached"
            else:
                self.last_error = "Weather service error %s" % e.code
            log.warning("Weather fetch failed: %s", self.last_error)
        except Exception as e:
            self.last_error = "No connection to the weather service"
            log.warning("Weather fetch failed: %s", e)
        return self.section()

    def section(self):
        age = time.time() - self.fetched_at if self.fetched_at else None
        # Stale = the last try failed, or the data is older than two refresh periods.
        stale = bool(self.data) and (bool(self.last_error) or (age is not None and age > 2 * self.cfg.weather_interval + 10))
        s = {
            "ok": bool(self.data),
            "configured": self.configured(),
            "stale": stale,
            "fetched_at": self.fetched_at,
            "error": self.last_error,
            "units": self.cfg.units,
        }
        if self.data:
            s.update(self.data)
        return s
