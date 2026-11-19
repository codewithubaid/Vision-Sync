"""VisionSync Phase 5 reminders: offline SQLite and system-local dates.
Supports reminders only between now and one calendar month from now.
Tasks are a separate database; no old task is deleted by this module.
"""
import calendar
import re
import sqlite3
from datetime import datetime, date, time, timedelta
from pathlib import Path
import builtins as _builtins
import os as _os


def print(*args, **kwargs):  # NFR-SEC3: no reminder text in the system log
    if _os.environ.get("VISION_DEBUG") == "1":
        _builtins.print(*args, **kwargs)


BASE = Path(__file__).resolve().parent
REMINDER_DB = BASE / "vision_reminders.db"
NOTES_DB = BASE / "vision_notes.db"

def _connect(path):
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    return conn

def init_db():
    with _connect(REMINDER_DB) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            due_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            notified_at TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status,due_at)")

def max_due(now=None):
    now=now or datetime.now()
    year=now.year + (now.month == 12)
    month=(now.month % 12)+1
    day=min(now.day, calendar.monthrange(year,month)[1])
    return now.replace(year=year,month=month,day=day)

def add_reminder(title, due_at, now=None):
    now=now or datetime.now()
    title=str(title).strip().strip('.!')
    if not title or len(title)>500:
        raise ValueError('Reminder title must contain 1–500 characters.')
    due=datetime.fromisoformat(str(due_at)) if not isinstance(due_at,datetime) else due_at
    if (due.tzinfo is None)!=(now.tzinfo is None):
        raise ValueError('Use a local datetime without a timezone offset.')
    if not now < due <= max_due(now):
        raise ValueError('Reminder must be in the future, within one calendar month.')
    with _connect(REMINDER_DB) as conn:
        cur=conn.execute("INSERT INTO reminders(title,due_at,status,created_at) VALUES(?,?,?,?)",
             (title,due.isoformat(timespec='minutes'),'pending',now.isoformat(timespec='seconds')))
        ident=cur.lastrowid
    return get_reminder(ident)

def get_reminder(ident):
    with _connect(REMINDER_DB) as conn:
        row=conn.execute("SELECT * FROM reminders WHERE id=?",(int(ident),)).fetchone()
    return dict(row) if row else None

def list_reminders(include_all=False):
    with _connect(REMINDER_DB) as conn:
        if include_all:
            rows=conn.execute('SELECT * FROM reminders ORDER BY due_at,id').fetchall()
        else:
            rows=conn.execute("SELECT * FROM reminders WHERE status='pending' ORDER BY due_at,id").fetchall()
    return [dict(x) for x in rows]

def delete_reminder(ident):
    with _connect(REMINDER_DB) as conn:
        cur=conn.execute('DELETE FROM reminders WHERE id=?',(int(ident),))
        return cur.rowcount>0

def due_reminders(now=None):
    """Claim due reminders atomically: each fires at most once across polls/restarts."""
    now=now or datetime.now()
    claimed=[]
    with _connect(REMINDER_DB) as conn:
        rows=conn.execute("SELECT * FROM reminders WHERE status='pending' AND due_at<=? ORDER BY due_at,id",
            (now.isoformat(timespec='minutes'),)).fetchall()
        for row in rows:
            c=conn.execute("UPDATE reminders SET status='notified',notified_at=? WHERE id=? AND status='pending'",
                (now.isoformat(timespec='seconds'),row['id']))
            if c.rowcount:
                claimed.append(dict(row))
    return claimed

def recent_tasks(days=7, pending_only=False, now=None):
    """Seven-day inclusive rolling calendar window (today + previous six days).
    Older tasks stay safely in SQLite but are hidden from the main task panel.
    """
    today=(now or datetime.now()).date()
    earliest=(today-timedelta(days=days-1)).isoformat()
    with _connect(NOTES_DB) as conn:
        where="note_date BETWEEN ? AND ?" + (" AND status='pending'" if pending_only else '')
        try:
            rows=conn.execute(f'SELECT id,note,note_date,created_at,status FROM notes WHERE {where} ORDER BY note_date,id',
                (earliest,today.isoformat())).fetchall()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc):
                return []
            raise
    return [dict(x) for x in rows]

# Normalize common spoken intervals before date parsing and title extraction.
# Keep this scoped to relative-time phrases so task/reminder titles are not changed.
_SPOKEN_INTERVAL_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
    "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45,
    "fifty": 50, "sixty": 60,
}

def _normalize_spoken_intervals(text):
    words = "|".join(sorted(map(re.escape, _SPOKEN_INTERVAL_NUMBERS), key=len, reverse=True))
    unit = r"(?:minutes?|mins?|hours?|hrs?)"
    # e.g. 'in ten minutes', 'after fifteen mins', 'ten minutes from now'.
    expression = re.compile(
        rf"\b(?:(in|after)\s+({words})\s+({unit})|({words})\s+({unit})\s+(from\s+now))\b",
        re.I,
    )
    def rewrite(match):
        if match.group(1):
            return f"{match.group(1)} {_SPOKEN_INTERVAL_NUMBERS[match.group(2).lower()]} {match.group(3)}"
        return f"{_SPOKEN_INTERVAL_NUMBERS[match.group(4).lower()]} {match.group(5)} {match.group(6)}"
    return expression.sub(rewrite, text)

# Controlled date parser for spoken reminders. Requires a clear reminder command.
def parse_when(text, now=None):
    now=now or datetime.now()
    value=_normalize_spoken_intervals(text).lower().strip().rstrip(' .!?')

    # Relative reminders use the system-local clock, not the 9 AM default.
    # Round UP to the next whole minute because SQLite stores due_at only
    # to minute precision; otherwise a reminder could fire early.
    interval = re.search(
        r'\b(?:in|after)\s+(\d+)\s*(minutes?|mins?|hours?|hrs?)\b'
        r'|\b(?:in|after)\s+(?:half\s+an?\s+hour|an?\s+hour)\b'
        r'|\b(\d+)\s*(minutes?|mins?|hours?|hrs?)\s+from\s+now\b',
        value,
    )
    if interval:
        if interval.group(1) or interval.group(3):
            count = int(interval.group(1) or interval.group(3))
            unit = interval.group(2) or interval.group(4)
            minutes = count * (60 if unit.startswith(('hour', 'hr')) else 1)
        else:
            phrase = interval.group(0)
            minutes = 30 if 'half' in phrase else 60
        if minutes < 1:
            return None
        when = now + timedelta(minutes=minutes)
        if when.second or when.microsecond:
            when = when.replace(second=0, microsecond=0) + timedelta(minutes=1)
        return when

    match=re.search(r'\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b',value)
    if match:
        hour=int(match.group(1));minute=int(match.group(2) or 0)
        if hour<1 or hour>12 or minute>59:
            return None
        ampm=match.group(3).replace('.','')
        hour=(hour%12)+(12 if ampm=='pm' else 0)
        value=value[:match.start()]+' '+value[match.end():]
    else:
        hour,minute=9,0  # explicit documented default
    value=re.sub(r'\s+',' ',value).strip()
    if re.search(r'\bday after tomorrow\b',value):
        day=now.date()+timedelta(days=2)
    elif re.search(r'\btomorrow\b',value):
        day=now.date()+timedelta(days=1)
    elif re.search(r'\btoday\b',value):
        day=now.date()
    elif (m:=re.search(r'\bin\s+(\d+)\s+days?\b',value)):
        day=now.date()+timedelta(days=int(m.group(1)))
    elif (m:=re.search(r'\bon\s+(\d{4})-(\d{2})-(\d{2})\b',value)):
        try: day=date(int(m.group(1)),int(m.group(2)),int(m.group(3)))
        except ValueError:return None
    elif (m:=re.search(r'\bon\s+([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b',value)):
        names={name.lower():i for i,name in enumerate(calendar.month_name) if name}
        names.update({name.lower():i for i,name in enumerate(calendar.month_abbr) if name})
        month=names.get(m.group(1))
        if not month:return None
        year=int(m.group(3)) if m.group(3) else now.year
        try:
            day=date(year,month,int(m.group(2)))
            if not m.group(3) and day<now.date():day=date(year+1,month,int(m.group(2)))
        except ValueError:return None
    else:
        return None
    return datetime.combine(day,time(hour,minute))

# In-memory, one-user follow-up state. Reset at conversation end.
# Never treat AI-generated promises as evidence that a reminder exists.
_pending_reminder = None


def reset_reminder_context():
    global _pending_reminder
    _pending_reminder = None


def _extract_title(content):
    content = _normalize_spoken_intervals(content)
    content = re.sub(
        r'\b(?:in|after)\s+(?:\d+\s*(?:minutes?|mins?|hours?|hrs?)|half\s+an?\s+hour|an?\s+hour)\b'
        r'|\b\d+\s*(?:minutes?|mins?|hours?|hrs?)\s+from\s+now\b',
        '', content, flags=re.I,
    )
    # Strip leftover leading 'to' from 'in 10 minutes to drink water'.
    title = re.sub(r'\b(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b', '', content, flags=re.I)
    title = re.sub(
        r'\b(?:on\s+\d{4}-\d{2}-\d{2}|on\s+[a-z]+\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?|in\s+\d+\s+days?|day after tomorrow|tomorrow|today)\b',
        '', title, flags=re.I,
    )
    title = re.sub(r'^(?:to|that)\s+', '', title.strip(' ,.:;!-'), flags=re.I)
    title = re.sub(r'\s+', ' ', title).strip(' ,.:;!-')
    return re.sub(r'^(?:about|of)\s+', '', title, flags=re.I)


def _format_saved(obj, explicit_time=True):
    due = datetime.fromisoformat(obj['due_at'])
    extra = '.' if explicit_time else ' (9 AM default time).'
    return f"Reminder {obj['id']} saved: {obj['title']} on {due.strftime('%B %d at %I:%M %p')}" + extra


def _schedule(title, when, now, explicit_time=True):
    global _pending_reminder
    try:
        obj = add_reminder(title, when, now)
    except ValueError as error:
        return str(error)
    # Read-back verification: don't claim success unless it actually exists.
    row = get_reminder(obj['id'])
    if not row or row['title'] != obj['title'] or row['due_at'] != obj['due_at']:
        return "I couldn't verify the reminder was saved. Please try again."
    _pending_reminder = None
    print(f"[REMINDER SAVED] id={row['id']} title={row['title']!r} due={row['due_at']}")
    return _format_saved(row, explicit_time)


def handle_reminder_command(message, now=None):
    global _pending_reminder
    now = now or datetime.now()
    text = _normalize_spoken_intervals(str(message).strip())
    lower = text.lower().strip().rstrip(' .!?')
    time_is_explicit = bool(re.search(
        r'\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b'
        r'|\b(?:in|after)\s+(?:\d+\s*(?:minutes?|mins?|hours?|hrs?)|half\s+an?\s+hour|an?\s+hour)\b'
        r'|\b\d+\s*(?:minutes?|mins?|hours?|hrs?)\s+from\s+now\b', text, re.I))

    # Read-only reminder questions MUST use SQLite, never Gemini/Qwen.
    # Avoid treating 'set a reminder' as a list query.
    reminder_query = (
        r"\b(?:what|which)\s+(?:are\s+)?(?:my\s+)?(?:upcoming\s+|pending\s+)?reminders?\b",
        r"\bdo\s+i\s+have\s+(?:any\s+)?(?:upcoming\s+|pending\s+)?reminders?\b",
        r"\b(?:tell\s+me|show\s+me|list|read|check|show)\b.{0,70}\breminders?\b",
        r"\bhow\s+many\s+(?:upcoming\s+|pending\s+)?reminders?\b",
        r"\b(?:my|upcoming|pending)\s+reminders?\b",
        r"\b(?:is|are)\s+there\s+(?:any\s+)?reminders?\b",
    )
    asking_for_reminders = any(re.search(pattern, lower) for pattern in reminder_query)
    creating_reminder = bool(re.search(
        r"\b(?:remind\s+me|set\s+(?:(?:a|the|new)\s+)*reminder|"
        r"create\s+(?:(?:a|the|new)\s+)*reminder|"
        r"add\s+(?:(?:a|the|new)\s+)*reminder|schedule\s+(?:(?:a|the|new)\s+)*reminder)\b",
        lower
    ))
    modifying_reminder = bool(re.search(
        r"\b(?:delete|remove|cancel|complete)\s+(?:my\s+)?reminder\b",
        lower
    ))
    if asking_for_reminders and not creating_reminder and not modifying_reminder:
        rows = list_reminders()
        print(f"[REMINDER DB READ] count={len(rows)} question={text!r}")
        if not rows:
            return "You have no upcoming reminders."
        return "Your upcoming reminders are: " + ". ".join(
            f"{i}. {item['title']}, "
            f"{datetime.fromisoformat(item['due_at']).strftime('%B %d at %I:%M %p')}"
            for i, item in enumerate(rows, 1)
        ) + "."

    if lower in {'cancel reminder', 'never mind', 'nevermind', 'cancel that reminder'}:
        if _pending_reminder:
            _pending_reminder = None
            return "Okay, I canceled that unsaved reminder."

    if re.fullmatch(r'(?:show|list|read|tell me|what are)\s+(?:my\s+)?(?:upcoming\s+)?reminders(?:\s+for\s+(?:the\s+)?next\s+month)?', lower) or lower in {'my reminders','what reminders do i have'}:
        rows=list_reminders()
        if not rows:return 'You have no upcoming reminders.'
        return 'Your upcoming reminders are: '+'. '.join(
            f"{i}. {x['title']}, {datetime.fromisoformat(x['due_at']).strftime('%B %d at %I:%M %p')}"
            for i,x in enumerate(rows,1)) +'.'

    # Reminder deletion must be handled here before the task/note router.
    # "Delete my reminder number one" refers to the first item in the
    # upcoming-reminders list, NOT a task number or the SQLite row ID.
    deleting_reminder = bool(re.search(
        r"\b(?:delete|remove|cancel)\b.*\breminders?\b", lower, re.I
    ))
    if deleting_reminder:
        rows = list_reminders()
        if not rows:
            return "You have no upcoming reminders to delete."

        # Convert spoken ordinal/cardinal words to the same 1-based ordering
        # used by the upcoming-reminders response and dashboard.
        number_words = {
            "one": 1, "first": 1, "two": 2, "second": 2,
            "three": 3, "third": 3, "four": 4, "fourth": 4,
            "five": 5, "fifth": 5, "six": 6, "sixth": 6,
            "seven": 7, "seventh": 7, "eight": 8, "eighth": 8,
            "nine": 9, "ninth": 9, "ten": 10, "tenth": 10,
        }
        ordinal_match = re.search(
            r"\b(?:reminder\s+(?:number\s+)?|number\s+)"
            r"(first|second|third|fourth|fifth|sixth|seventh|eighth|"
            r"ninth|tenth|one|two|three|four|five|six|seven|eight|"
            r"nine|ten|\d+)\b"
            r"|\b(first|second|third|fourth|fifth|sixth|seventh|"
            r"eighth|ninth|tenth)\s+reminder\b",
            lower
        )

        # A date can identify one reminder, or several reminders on that day.
        target_day = None
        if re.search(r"\bday after tomorrow\b", lower):
            target_day = now.date() + timedelta(days=2)
        elif re.search(r"\btomorrow\b", lower):
            target_day = now.date() + timedelta(days=1)
        elif re.search(r"\btoday\b", lower):
            target_day = now.date()
        elif (m := re.search(r"\b(?:on|for)\s+(\d{4})-(\d{2})-(\d{2})\b", lower)):
            try:
                target_day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return "I couldn't understand that reminder date."
        elif (m := re.search(
            r"\b(?:on|for)\s+([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b",
            lower
        )):
            month_names = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
            month_names.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
            month = month_names.get(m.group(1))
            if month:
                year = int(m.group(3)) if m.group(3) else now.year
                try:
                    target_day = date(year, month, int(m.group(2)))
                    if not m.group(3) and target_day < now.date():
                        target_day = date(year + 1, month, int(m.group(2)))
                except ValueError:
                    return "I couldn't understand that reminder date."
        elif (m := re.search(r"\bin\s+(\d+)\s+days?\b", lower)):
            target_day = now.date() + timedelta(days=int(m.group(1)))

        candidates = rows
        if target_day is not None:
            candidates = [
                item for item in rows
                if datetime.fromisoformat(item["due_at"]).date() == target_day
            ]
            if not candidates:
                return f"You have no upcoming reminders for {target_day.strftime('%B %d')}."

        # If the user named a reminder rather than a number/date, match its title.
        if target_day is None and ordinal_match is None:
            query = re.sub(
                r"^(?:please\s+)?(?:delete|remove|cancel)\s+"
                r"(?:my\s+)?reminders?\s*", "", text, flags=re.I
            ).strip(" .,!?:;")
            if query:
                matches = [
                    item for item in rows
                    if query.lower() in item["title"].lower()
                ]
                if len(matches) == 1:
                    candidates = matches
                elif len(matches) > 1:
                    options = "; ".join(
                        f"{i}. {item['title']}"
                        for i, item in enumerate(matches, 1)
                    )
                    return f"I found multiple matching reminders: {options}. Which number should I delete?"
                else:
                    return f"I couldn't find an upcoming reminder matching '{query}'."

        if ordinal_match:
            token = ordinal_match.group(1) or ordinal_match.group(2)
            index = number_words.get(token, int(token) if token.isdigit() else None)
            # When a date was specified, allow the ordinal to select within
            # that day's matching reminders; otherwise use the full list.
            if index is None or index < 1 or index > len(candidates):
                return f"I couldn't find reminder number {token}."
            selected = candidates[index - 1]
        elif len(candidates) == 1:
            selected = candidates[0]
        elif len(candidates) > 1:
            options = "; ".join(
                f"{i}. {item['title']} at "
                f"{datetime.fromisoformat(item['due_at']).strftime('%I:%M %p')}"
                for i, item in enumerate(candidates, 1)
            )
            return (
                f"There are {len(candidates)} reminders on "
                f"{target_day.strftime('%B %d')}: {options}. "
                "Which number should I delete?"
            )
        else:
            # Try matching the reminder title after removing the command words.
            query = re.sub(
                r"^(?:please\s+)?(?:delete|remove|cancel)\s+"
                r"(?:my\s+)?reminders?\s*", "", text, flags=re.I
            ).strip(" .,!?:;")
            query = re.sub(
                r"\b(?:for|on)\s+(?:tomorrow|today|day after tomorrow|"
                r"[a-z]+\s+\d{1,2}(?:st|nd|rd|th)?|\d{4}-\d{2}-\d{2})\b",
                "", query, flags=re.I
            ).strip(" .,!?:;")
            if query:
                matches = [
                    item for item in rows
                    if query.lower() in item["title"].lower()
                ]
                if len(matches) == 1:
                    selected = matches[0]
                elif len(matches) > 1:
                    options = "; ".join(
                        f"{i}. {item['title']}"
                        for i, item in enumerate(matches, 1)
                    )
                    return f"I found multiple matching reminders: {options}. Which number should I delete?"
                else:
                    return f"I couldn't find an upcoming reminder matching '{query}'."
            else:
                return "Which reminder should I delete? Say its number, title, or date."

        if delete_reminder(selected["id"]):
            print(
                f"[REMINDER DELETED] id={selected['id']} "
                f"title={selected['title']!r}"
            )
            return f"Deleted reminder: {selected['title']}."
        return "I couldn't delete that reminder. Please try again."

    # Follow-up to a reminder that was missing either a date or activity.
    # Date-only utterances must not go to the LLM for an invented confirmation.
    if _pending_reminder is not None:
        pending = _pending_reminder
        if pending.get('title') and not pending.get('when'):
            # "It is on November 21, 10 a.m."
            when = parse_when(text,now)
            if when:
                return _schedule(pending['title'], when, now, time_is_explicit)
            if re.search(r'\b(?:on|tomorrow|today|in\s+\d+\s+days?)\b',lower):
                return 'I still need a clear date within the next month, such as November 21 at 10 AM.'
        elif pending.get('when') and not pending.get('title'):
            # User gives just the activity after giving the date.
            if not re.search(r'^(?:remind me|set a reminder)',lower):
                title = re.sub(r'^(?:to|remind me to|it is to)\s+', '',text,flags=re.I).strip(' .!?')
                if title and not parse_when(text, now):
                    return _schedule(title, pending['when'], now, pending.get('explicit_time',False))

    # A date response without an unsaved reminder should not be handled by AI.
    if re.match(r'^(?:it(?:\s+is|\'s)?\s+)?(?:on\s+[a-z]+\s+\d{1,2}|tomorrow|today|on\s+\d{4}-\d{2}-\d{2})\b', lower):
        if parse_when(text,now):
            return 'What should I remind you about? Please say the activity and date together.'

    # Accept natural spoken prefixes such as "Vision, remind me...",
    # "Hey Vision, can you remind me...", and "Please remind me...".
    # Deletion/list handlers above remain unchanged. Search for the explicit
    # creation phrase instead of requiring it to be the first transcript token.
    creation_match = re.search(
        r"\b(?:please\s+)?(?:remind me|set\s+(?:(?:a|the|new)\s+)*reminder|"
        r"create\s+(?:(?:a|the|new)\s+)*reminder|"
        r"add\s+(?:(?:a|the|new)\s+)*reminder|"
        r"schedule\s+(?:(?:a|the|new)\s+)*reminder)\b",
        text, flags=re.I
    )
    if not creation_match:
        return None

    # New reminder request always supersedes an unfinished request.
    content = text[creation_match.end():].strip()
    # Normalize common spoken forms: "for me that I have to eat..."
    content = re.sub(
        r'^(?:for\s+me\s+)?(?:that\s+)?i\s+(?:have|need|want)\s+to\s+',
        '', content, flags=re.I)
    content = re.sub(
        r'^(?:for\s+me\s+)?that\s+i\s+(?:have|need|want)\s+to\s+',
        '', content, flags=re.I)
    content = re.sub(r'^(?:for\s+me\s+)?(?:to|that)\s+','',content,flags=re.I)
    when = parse_when(content,now)
    title = _extract_title(content)
    about = bool(re.match(r'(?:about|of)\b', content.strip(), re.I))
    if when is None:
        if title:
            _pending_reminder = {'title':title,'when':None}
            return f"When should I remind you {'about' if about else 'to'} {title}? Give a date within the next month and an optional time."
        _pending_reminder = None
        return 'Please tell me what to remind you about and when.'
    if not title:
        _pending_reminder = {'title':None,'when':when,'explicit_time':time_is_explicit}
        return f"What should I remind you about on {when.strftime('%B %d at %I:%M %p')}?"
    return _schedule(title,when,now,time_is_explicit)

init_db()
