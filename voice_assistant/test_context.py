"""Run in a disposable folder so existing VisionSync data is never mutated."""
import os
import sys
import tempfile
import shutil
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
with tempfile.TemporaryDirectory(prefix='visionsync_context_test_') as tmp:
    target = Path(tmp)
    for name in ('ai_router.py','context_manager.py','notes.py','memory.py'):
        shutil.copyfile(SOURCE/name, target/name)
    sys.path.insert(0, str(target))
    os.environ['VISION_ENABLE_GEMINI'] = '0'
    import ai_router as router
    from notes import get_pending_notes, get_all_notes, add_note
    from context_manager import context
    router.ask_ollama = lambda messages, model, first_token_timeout=None: 'Okay, I understand.'

    def say(utterance):
        result = router.ask_ai(utterance)
        print(f'{utterance!r} -> {result!r}')
        return result

    # Explicitly verbalized task candidates may be added by short reference.
    say('I need to buy milk')
    assert 'Added and verified' in say('Add that to my tasks')
    assert [r[1] for r in get_pending_notes()] == ['Buy milk']
    assert 'already in' in say('Add it to my tasks') if context.pending_description else True
    say('Mark that as done')
    assert not get_pending_notes()
    assert get_all_notes()[0][-1] == 'completed'

    # The item just added is uniquely identifiable, and can be removed by reference.
    say('I want to visit the park')
    assert 'Added and verified' in say('Add that to my tasks')
    assert 'Deleted' in say('Delete that task')
    assert len(get_all_notes()) == 1

    # A list of multiple tasks is never a safe antecedent for "that".
    add_note('Cut vegetables')
    add_note('Go shopping')
    assert 'tasks for today' in say('What are my tasks for today?').lower()
    assert 'Which task' in say('Mark that done')
    assert len(get_pending_notes()) == 2

    # The following day query must not accidentally show today's tasks.
    assert 'no pending tasks for tomorrow' in say('What about tomorrow?')

    # Chat reset must drop short-term context, not erase persistent records.
    router.reset_conversation_history()
    assert context.focus_id is None and context.pending_description is None
    assert 'Which task' in say('Delete that')
    assert len(get_pending_notes()) == 2

    print('ALL PHASE 3 ISOLATED TESTS PASSED')
