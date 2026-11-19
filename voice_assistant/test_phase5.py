"""Run: python test_phase5.py
Uses a temporary directory and does not alter your personal databases.
"""
import importlib.util
import sqlite3
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timedelta

HERE = Path(__file__).resolve().parent

def run():
    with tempfile.TemporaryDirectory() as tmp:
        location = Path(tmp)
        shutil.copy(HERE / 'reminder_manager.py', location / 'reminder_manager.py')
        spec = importlib.util.spec_from_file_location('test_reminders', location / 'reminder_manager.py')
        r = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(r)
        now = datetime(2026, 11, 9, 8, 0)
        assert r.max_due(now) == datetime(2026, 12, 9, 8, 0)
        assert r.add_reminder('Pay rent', datetime(2026,12,9,8), now)['title'] == 'Pay rent'
        try:
            r.add_reminder('Too late',datetime(2026,12,10,8),now)
            raise AssertionError('Outside range accepted')
        except ValueError:
            pass
        assert 'saved' in r.handle_reminder_command('Remind me to buy milk tomorrow at 9 AM', now).lower()
        assert len(r.list_reminders()) == 2
        assert len(r.due_reminders(datetime(2026,11,10,9,1))) == 1
        assert len(r.due_reminders(datetime(2026,11,10,9,2))) == 0
        assert len(r.list_reminders()) == 1
        assert r.delete_reminder(1)
        assert len(r.list_reminders()) == 0
        with sqlite3.connect(location/'vision_notes.db') as con:
            con.execute('CREATE TABLE notes (id INTEGER PRIMARY KEY,note TEXT,note_date TEXT,created_at TEXT,status TEXT)')
            for i,days_ago in enumerate((0,6,7),1):
                day = (now.date()-timedelta(days=days_ago)).isoformat()
                con.execute('INSERT INTO notes VALUES (?,?,?,?,?)', (i,'Task '+str(i),day,day,'pending'))
        recent = r.recent_tasks(now=now)
        assert {row['id'] for row in recent} == {1,2}
        # Old task still physically exists; it is hidden, never deleted.
        with sqlite3.connect(location/'vision_notes.db') as con:
            assert con.execute('SELECT count(*) FROM notes').fetchone()[0] == 3
    print('PASS: one-month limit, reminder CRUD, one-shot alerts, seven-day task history and preservation')

if __name__ == '__main__':
    run()
