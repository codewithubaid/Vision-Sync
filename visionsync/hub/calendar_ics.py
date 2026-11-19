"""
calendar_ics.py - Google Calendar through its secret iCal address.

No OAuth screens, no extra Python packages: this file reads the .ics text,
expands repeating events, and returns the next events. On failure the widget
shows the SRS message 'Calendar unavailable, check connection.' and keeps
the last events marked as stale (UC-1 alternate courses 1 and 2).
"""

import calendar as _cal
import logging
import os
import re
import time
from datetime import datetime, timedelta, date

try:
    from urllib.request import urlopen, Request
except ImportError:  # pragma: no cover
    raise

log = logging.getLogger("hub.calendar")

UNAVAILABLE = "Calendar unavailable, check connection."
_WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
_MAX_STEPS = 20000


# ---------- time zones ----------
def _get_tz(tzid):
    if not tzid:
        return None
    try:
        from zoneinfo import ZoneInfo          # Python 3.9+
        return ZoneInfo(tzid)
    except Exception:
        pass
    try:
        from dateutil import tz as _tz         # python3-dateutil, usually installed
        return _tz.gettz(tzid)
    except Exception:
        return None


def to_epoch(naive, tzid=None, utc=False):
    """Naive wall-clock datetime -> Unix seconds."""
    if utc:
        return _cal.timegm(naive.timetuple())
    tz = _get_tz(tzid)
    if tz is not None:
        try:
            return int(naive.replace(tzinfo=tz).timestamp())
        except Exception:
            pass
    return int(time.mktime(naive.timetuple()))   # mirror's own time zone


# ---------- reading the file ----------
def _unfold(text):
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _split_line(line):
    """'DTSTART;TZID=Asia/Karachi:20260101T090000' -> (name, params, value)"""
    in_quote = False
    idx = -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_quote = not in_quote
        elif ch == ":" and not in_quote:
            idx = i
            break
    if idx < 0:
        return None, {}, ""
    head, value = line[:idx], line[idx + 1:]
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def _text(v):
    return (v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",")
             .replace("\\;", ";").replace("\\\\", "\\").strip())


def _parse_dt(value, params):
    """Returns (naive datetime, all_day, utc, tzid) or None."""
    value = value.strip()
    try:
        if params.get("VALUE", "").upper() == "DATE" or re.match(r"^\d{8}$", value):
            return datetime.strptime(value[:8], "%Y%m%d"), True, False, None
        utc = value.endswith("Z")
        return datetime.strptime(value[:15], "%Y%m%dT%H%M%S"), False, utc, params.get("TZID")
    except ValueError:
        return None


def _parse_duration(value):
    m = re.match(r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$", value.strip())
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    w, d, h, mi, s = [int(x) if x else 0 for x in m.groups()[1:]]
    return sign * timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def parse_events(text):
    """All VEVENT blocks as dicts (not yet expanded)."""
    events, cur, depth = [], None, 0
    for line in _unfold(text):
        name, params, value = _split_line(line)
        if not name:
            continue
        if name == "BEGIN":
            if value.upper() == "VEVENT":
                cur = {"exdates": [], "raw_start": None}
            elif cur is not None:
                depth += 1               # e.g. VALARM inside the event
            continue
        if name == "END":
            if value.upper() == "VEVENT" and cur is not None:
                if cur.get("start"):
                    events.append(cur)
                cur, depth = None, 0
            elif cur is not None and depth:
                depth -= 1
            continue
        if cur is None or depth:
            continue
        if name == "SUMMARY":
            cur["title"] = _text(value)
        elif name == "LOCATION":
            cur["location"] = _text(value)
        elif name == "UID":
            cur["uid"] = value.strip()
        elif name == "STATUS":
            cur["status"] = value.strip().upper()
        elif name == "DTSTART":
            cur["start"] = _parse_dt(value, params)
        elif name == "DTEND":
            cur["end"] = _parse_dt(value, params)
        elif name == "DURATION":
            cur["duration"] = _parse_duration(value)
        elif name == "RRULE":
            cur["rrule"] = dict(kv.split("=", 1) for kv in value.strip().upper().split(";") if "=" in kv)
        elif name == "EXDATE":
            for v in value.split(","):
                p = _parse_dt(v, params)
                if p:
                    cur["exdates"].append(p)
        elif name == "RECURRENCE-ID":
            cur["recurrence_id"] = _parse_dt(value, params)
    return events


# ---------- repeating events ----------
def _month_add(y, m, n):
    m0 = (m - 1) + n
    return y + m0 // 12, m0 % 12 + 1


def _nth_weekday(y, m, weekday, n):
    """n-th (1..5, or -1 = last) given weekday of a month, or None."""
    days = _cal.monthrange(y, m)[1]
    hits = [d for d in range(1, days + 1) if date(y, m, d).weekday() == weekday]
    try:
        return hits[n - 1] if n > 0 else hits[n]
    except IndexError:
        return None


def _occurrences(start, rule, window_start, window_end):
    """Yields naive start datetimes of a repeating event, in order.
    `window_start` is only a hint to skip far-past repeats quickly."""
    freq = rule.get("FREQ", "")
    interval = max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = None
    if rule.get("UNTIL"):
        u = _parse_dt(rule["UNTIL"], {})
        if u:
            until = u[0] if not u[1] else u[0] + timedelta(days=1) - timedelta(seconds=1)
            if u[2]:
                until += timedelta(hours=14)     # UTC vs local: be generous, never drop a real repeat
    byday = [d for d in rule.get("BYDAY", "").split(",") if d]
    bymonthday = [int(d) for d in rule.get("BYMONTHDAY", "").split(",") if d.lstrip("-").isdigit()]

    emitted = 0
    steps = 0

    def ok(dt):
        return (until is None or dt <= until) and dt <= window_end

    if freq == "DAILY":
        cur = start
        if count is None and window_start > start:
            skip = ((window_start - start).days // interval) - 1
            if skip > 0:
                cur = start + timedelta(days=skip * interval)
        allowed = [_WEEKDAYS.index(d[-2:]) for d in byday if d[-2:] in _WEEKDAYS]
        while ok(cur) and steps < _MAX_STEPS:
            steps += 1
            if not allowed or cur.weekday() in allowed:
                yield cur
                emitted += 1
                if count is not None and emitted >= count:
                    return
            cur += timedelta(days=interval)

    elif freq == "WEEKLY":
        days = sorted(_WEEKDAYS.index(d[-2:]) for d in byday if d[-2:] in _WEEKDAYS) or [start.weekday()]
        week0 = start - timedelta(days=start.weekday())
        if count is None and window_start > start:
            skip = ((window_start - start).days // (7 * interval)) - 1
            if skip > 0:
                week0 += timedelta(weeks=skip * interval)
        while steps < _MAX_STEPS:
            steps += 1
            if week0 > window_end or (until is not None and week0 > until):
                return
            for wd in days:
                cur = week0 + timedelta(days=wd)
                if cur < start:
                    continue
                if not ok(cur):
                    return
                yield cur
                emitted += 1
                if count is not None and emitted >= count:
                    return
            week0 += timedelta(weeks=interval)

    elif freq == "MONTHLY":
        y, m, n = start.year, start.month, 0
        while steps < _MAX_STEPS:
            steps += 1
            yy, mm = _month_add(y, m, n * interval)
            n += 1
            dim = _cal.monthrange(yy, mm)[1]
            day_list = []
            if byday:
                for d in byday:
                    mt = re.match(r"^(-?\d+)?([A-Z]{2})$", d)
                    if not mt or mt.group(2) not in _WEEKDAYS:
                        continue
                    wd = _WEEKDAYS.index(mt.group(2))
                    if mt.group(1):
                        hit = _nth_weekday(yy, mm, wd, int(mt.group(1)))
                        if hit:
                            day_list.append(hit)
                    else:
                        day_list += [x for x in range(1, dim + 1) if date(yy, mm, x).weekday() == wd]
            elif bymonthday:
                day_list = [x if x > 0 else dim + 1 + x for x in bymonthday]
            else:
                day_list = [start.day]
            first = start.replace(year=yy, month=mm, day=1)
            if first > window_end or (until is not None and first > until):
                return
            for dnum in sorted(set(day_list)):
                if dnum < 1 or dnum > dim:
                    continue
                cur = start.replace(year=yy, month=mm, day=dnum)
                if cur < start:
                    continue
                if not ok(cur):
                    return
                yield cur
                emitted += 1
                if count is not None and emitted >= count:
                    return

    elif freq == "YEARLY":
        n = 0
        while steps < _MAX_STEPS:
            steps += 1
            yy = start.year + n * interval
            n += 1
            try:
                cur = start.replace(year=yy)
            except ValueError:          # 29 February in a normal year
                if yy > window_end.year:
                    return
                continue
            if not ok(cur):
                return
            yield cur
            emitted += 1
            if count is not None and emitted >= count:
                return
    else:
        yield start


def upcoming(text, now=None, days_ahead=30, limit=5):
    """Next events from .ics text: list of dicts sorted by start time."""
    now = int(now if now is not None else time.time())
    window_end_epoch = now + days_ahead * 86400
    now_naive = datetime.fromtimestamp(now)
    win_start = now_naive - timedelta(days=2)
    win_end = now_naive + timedelta(days=days_ahead + 2)

    parsed = parse_events(text)

    # A changed single repeat (RECURRENCE-ID) replaces the original repeat.
    overridden = set()
    for ev in parsed:
        rid = ev.get("recurrence_id")
        if rid and ev.get("uid"):
            overridden.add((ev["uid"], to_epoch(rid[0], rid[3], rid[2])))

    out = []
    for ev in parsed:
        if ev.get("status") == "CANCELLED":
            continue
        start, all_day, utc, tzid = ev["start"]
        if ev.get("end"):
            length = ev["end"][0] - start
        elif ev.get("duration"):
            length = ev["duration"]
        else:
            length = timedelta(days=1) if all_day else timedelta(0)
        if length < timedelta(0):
            length = timedelta(0)

        if ev.get("rrule") and not ev.get("recurrence_id"):
            starts = _occurrences(start, ev["rrule"], win_start - length, win_end)
        else:
            starts = [start]
        excluded = set(to_epoch(x[0], x[3], x[2]) for x in ev["exdates"])

        for s in starts:
            s_epoch = to_epoch(s, tzid, utc)
            if s_epoch > window_end_epoch:
                if ev.get("rrule"):
                    break
                continue
            e_epoch = to_epoch(s + length, tzid, utc)
            if all_day:
                e_epoch -= 1                       # all-day end is "the next midnight"
            if max(e_epoch, s_epoch) <= now:
                continue
            if s_epoch in excluded:
                continue
            if ev.get("rrule") and not ev.get("recurrence_id") and (ev.get("uid"), s_epoch) in overridden:
                continue
            out.append({
                "title": ev.get("title") or "(No title)",
                "start": s_epoch,
                "end": e_epoch,
                "all_day": all_day,
                "location": ev.get("location", ""),
            })

    out.sort(key=lambda e: (e["start"], e["title"]))
    return out[:limit]


# ---------- service ----------
def _get_text(url, timeout=10):
    req = Request(url, headers={"User-Agent": "VisionSync/1.0"})
    resp = urlopen(req, timeout=timeout)
    try:
        return resp.read().decode("utf-8", "replace")
    finally:
        resp.close()


class CalendarService(object):
    def __init__(self, cfg, fetch=_get_text):
        self.cfg = cfg
        self._fetch = fetch
        self.cache_path = os.path.join(cfg.cache_dir, "calendar.ics")
        self.text = None
        self.fetched_at = 0
        self.last_error = ""
        try:
            with open(self.cache_path, "r") as f:
                self.text = f.read()
            self.fetched_at = int(os.path.getmtime(self.cache_path))
        except (IOError, OSError):
            pass

    def configured(self):
        return bool(self.cfg.calendar_url)

    def refresh(self):
        if not self.configured():
            self.last_error = "Add GOOGLE_CALENDAR_ICS_URL to .env"
            return self.section()
        try:
            text = self._fetch(self.cfg.calendar_url)
            if "BEGIN:VCALENDAR" not in text:
                raise ValueError("not a calendar file")
            self.text = text
            self.fetched_at = int(time.time())
            self.last_error = ""
            try:
                if not os.path.isdir(self.cfg.cache_dir):
                    os.makedirs(self.cfg.cache_dir)
                tmp = self.cache_path + ".tmp"
                with open(tmp, "w") as f:
                    f.write(text)
                os.chmod(tmp, 0o600)
                os.rename(tmp, self.cache_path)
            except (IOError, OSError) as e:
                log.warning("Could not save the calendar cache: %s", e)
        except Exception as e:
            self.last_error = UNAVAILABLE
            log.warning("Calendar fetch failed: %s", e)
        return self.section()

    def section(self):
        """Re-computed often, so finished events drop off without a new download."""
        events = []
        if self.text:
            try:
                events = upcoming(self.text, days_ahead=self.cfg.calendar_days_ahead,
                                  limit=self.cfg.calendar_max_events)
            except Exception as e:      # a broken file must never stop the hub
                log.error("Calendar could not be read: %s", e)
                self.last_error = UNAVAILABLE
        age = time.time() - self.fetched_at if self.fetched_at else None
        stale = bool(self.text) and (bool(self.last_error) or (age is not None and age > 2 * self.cfg.calendar_interval + 10))
        return {
            "ok": bool(self.text),
            "configured": self.configured(),
            "stale": stale,
            "fetched_at": self.fetched_at,
            "error": self.last_error,
            "events": events,
        }
