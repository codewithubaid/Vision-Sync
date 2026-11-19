"""
VisionSync Notes and Task Manager.
Cross-platform SQLite storage for Windows and NVIDIA Jetson Linux.

The original database schema is preserved. Older databases are migrated
automatically by adding task status and completion timestamp columns.
"""

import sqlite3
from datetime import datetime, date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATABASE = str(BASE_DIR / "vision_notes.db")


def _connect():
    conn = sqlite3.connect(DATABASE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def create_database():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                note TEXT NOT NULL,
                note_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(notes)").fetchall()
        }

        if "status" not in columns:
            conn.execute(
                "ALTER TABLE notes ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'"
            )

        if "completed_at" not in columns:
            conn.execute(
                "ALTER TABLE notes ADD COLUMN completed_at TEXT"
            )

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_notes_date_status
            ON notes(note_date, status)
        """)


def add_note(note, note_date=None):
    """Add one task and return its database ID."""
    if not isinstance(note, str):
        raise TypeError("Note must be text.")

    note = note.strip()
    if not note:
        raise ValueError("Note cannot be empty.")

    if note_date is None:
        note_date = date.today().isoformat()

    date.fromisoformat(note_date)

    with _connect() as conn:
        cursor = conn.execute("""
            INSERT INTO notes (note, note_date, created_at, status)
            VALUES (?, ?, ?, 'pending')
        """, (note, note_date, datetime.now().isoformat(timespec="seconds")))

        return cursor.lastrowid


def get_today_notes():
    """Backward-compatible: pending notes as (id, note, created_at)."""
    return get_pending_notes(date.today().isoformat())


def get_notes_for_date(note_date):
    """Backward-compatible: all notes for a date."""
    date.fromisoformat(note_date)

    with _connect() as conn:
        rows = conn.execute("""
            SELECT id, note, created_at
            FROM notes
            WHERE note_date = ?
            ORDER BY id
        """, (note_date,)).fetchall()

    return [(r["id"], r["note"], r["created_at"]) for r in rows]


def get_pending_notes(note_date=None):
    if note_date is None:
        note_date = date.today().isoformat()

    date.fromisoformat(note_date)

    with _connect() as conn:
        rows = conn.execute("""
            SELECT id, note, created_at
            FROM notes
            WHERE note_date = ? AND status = 'pending'
            ORDER BY id
        """, (note_date,)).fetchall()

    return [(r["id"], r["note"], r["created_at"]) for r in rows]


def get_all_notes(note_date=None):
    """Return (id, note, note_date, created_at, status) records."""
    with _connect() as conn:
        if note_date is None:
            rows = conn.execute("""
                SELECT id, note, note_date, created_at, status
                FROM notes ORDER BY note_date DESC, id
            """).fetchall()
        else:
            date.fromisoformat(note_date)
            rows = conn.execute("""
                SELECT id, note, note_date, created_at, status
                FROM notes WHERE note_date = ? ORDER BY id
            """, (note_date,)).fetchall()

    return [
        (r["id"], r["note"], r["note_date"], r["created_at"], r["status"])
        for r in rows
    ]


def get_note(note_id):
    with _connect() as conn:
        row = conn.execute("""
            SELECT id, note, note_date, created_at, status, completed_at
            FROM notes WHERE id = ?
        """, (int(note_id),)).fetchone()

    return dict(row) if row else None


def complete_note(note_id):
    with _connect() as conn:
        cursor = conn.execute("""
            UPDATE notes
            SET status = 'completed', completed_at = ?
            WHERE id = ? AND status != 'completed'
        """, (datetime.now().isoformat(timespec="seconds"), int(note_id)))

    return cursor.rowcount > 0


def reopen_note(note_id):
    with _connect() as conn:
        cursor = conn.execute("""
            UPDATE notes
            SET status = 'pending', completed_at = NULL
            WHERE id = ? AND status != 'pending'
        """, (int(note_id),))

    return cursor.rowcount > 0


def delete_note(note_id):
    """Backward-compatible deletion by note ID."""
    return delete_note_by_id(note_id)


def delete_note_by_id(note_id):
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM notes WHERE id = ?", (int(note_id),)
        )

    return cursor.rowcount > 0


def find_notes(query, note_date=None):
    query = str(query).strip()
    if not query:
        return []

    pattern = f"%{query}%"

    with _connect() as conn:
        if note_date is None:
            rows = conn.execute("""
                SELECT id, note, note_date, created_at, status
                FROM notes
                WHERE note LIKE ? COLLATE NOCASE
                ORDER BY note_date DESC, id
            """, (pattern,)).fetchall()
        else:
            date.fromisoformat(note_date)
            rows = conn.execute("""
                SELECT id, note, note_date, created_at, status
                FROM notes
                WHERE note LIKE ? COLLATE NOCASE AND note_date = ?
                ORDER BY id
            """, (pattern, note_date)).fetchall()

    return [
        (r["id"], r["note"], r["note_date"], r["created_at"], r["status"])
        for r in rows
    ]


def clear_completed_notes(note_date=None):
    with _connect() as conn:
        if note_date is None:
            cursor = conn.execute(
                "DELETE FROM notes WHERE status = 'completed'"
            )
        else:
            date.fromisoformat(note_date)
            cursor = conn.execute("""
                DELETE FROM notes
                WHERE status = 'completed' AND note_date = ?
            """, (note_date,))

    return cursor.rowcount


def delete_all_notes(note_date=None):
    with _connect() as conn:
        if note_date is None:
            cursor = conn.execute("DELETE FROM notes")
        else:
            date.fromisoformat(note_date)
            cursor = conn.execute(
                "DELETE FROM notes WHERE note_date = ?", (note_date,)
            )

    return cursor.rowcount


create_database()
