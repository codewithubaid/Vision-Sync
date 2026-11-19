import importlib.util, tempfile, shutil
from pathlib import Path
from datetime import datetime

SOURCE=Path(__file__).resolve().parent/'reminder_manager.py'
with tempfile.TemporaryDirectory() as td:
    loc=Path(td)
    shutil.copy(SOURCE,loc/'reminder_manager.py')
    spec=importlib.util.spec_from_file_location('test_reminder_manager',loc/'reminder_manager.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    now=datetime(2026,11,9,6,30)
    reply=module.handle_reminder_command('Remind me to pay my bill',now)
    assert 'when should' in reply.lower(),reply
    assert module.list_reminders()==[]
    reply=module.handle_reminder_command('It is on November 21, 10 a.m.',now)
    print('followup:',reply)
    assert 'saved' in reply.lower(),reply
    saved=module.list_reminders()
    assert len(saved)==1 and saved[0]['title']=='pay my bill' and saved[0]['due_at']=='2026-11-21T10:00',saved
    assert 'pay my bill' in module.handle_reminder_command('Tell me my reminders',now)
    module.reset_reminder_context()
    assert module.handle_reminder_command('It is on November 22, 10 a.m.',now).startswith('What should'), 'Date-alone falsely saved'
    assert len(module.list_reminders())==1
    reply=module.handle_reminder_command('Remind me to buy groceries on November 25 at 9 AM',now)
    assert 'saved' in reply.lower(),reply
    reply=module.handle_reminder_command('Remind me on November 28 at 7 PM',now)
    assert 'what should' in reply.lower(),reply
    reply=module.handle_reminder_command('Call my friend',now)
    assert 'saved' in reply.lower(),reply
    assert len(module.list_reminders())==3
    reply=module.handle_reminder_command('Remind me to pay rent',now)
    assert 'when should' in reply.lower()
    reply=module.handle_reminder_command('Cancel reminder',now)
    assert 'canceled' in reply.lower()
    assert len(module.list_reminders())==3
    reply=module.handle_reminder_command('Remind me to call the dentist on January 1 at 10 AM',now)
    assert 'within one calendar month' in reply.lower(),reply
    assert len(module.list_reminders())==3
    print('PASS: follow-up date, no false confirmations, readback, cancel, one-month limit')
