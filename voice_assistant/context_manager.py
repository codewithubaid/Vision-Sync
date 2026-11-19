"""Conversation context for VisionSync.
Tracks only short-lived references needed to understand follow-up commands.
No persistent personal facts are recorded in this module.
"""
import re
from datetime import date, timedelta
from notes import get_pending_notes, get_all_notes, complete_note, delete_note_by_id, add_note

def normalize(text):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(text).casefold())).strip()

def _task_text(text):
    text = normalize(text)
    return re.sub(r"^(?:to|the|a|an|my)\s+", "", text).strip()

class ConversationContext:
    def __init__(self):
        self.reset()

    def reset(self):
        self.focus_id = None
        self.pending_description = None
        self.last_intent = None
        self.listed_ids = []
        self.last_date = None

    def _existing_task(self, task_id):
        for row in get_all_notes():
            if row[0] == task_id:
                return row
        return None

    def _pending_task(self, task_id):
        for row in get_pending_notes():
            if row[0] == task_id:
                return row
        return None

    def _choose_focus(self, description):
        if not description:
            return None
        needle = _task_text(description)
        rows = get_pending_notes()
        exact = [r for r in rows if _task_text(r[1]) == needle]
        if len(exact) == 1:
            return exact[0][0]
        return None

    def handle_followup(self, message):
        text = normalize(message)
        if text in {"cancel that", "never mind", "nevermind"}:
            self.pending_description = None
            return "Okay, I won't add that to your tasks."

        if re.fullmatch(r"(?:please )?(?:add|save|put|note|write) (?:that|it)(?: (?:to|in|on) (?:my )?(?:task|tasks|to do list|todo list|notes))?", text):
            if not self.pending_description:
                return "Which task should I add? Please say the task again."
            content = self.pending_description
            needle = _task_text(content)
            matches = [r for r in get_pending_notes() if _task_text(r[1]) == needle]
            if matches:
                self.focus_id = matches[0][0] if len(matches) == 1 else None
                self.last_intent = "add"
                self.pending_description = None
                return f"{matches[0][1]} is already in today's tasks."
            new_id = add_note(content)
            row = self._pending_task(new_id)
            if not row:
                return "I couldn't verify the new task, so I won't claim it was saved."
            self.focus_id = new_id
            self.last_intent = "add"
            self.pending_description = None
            return f"Added and verified: {row[1]}."

        # "Mark that done" or "delete it" should act only on a UNIQUE
        # previously discussed task, not the last item in a multi-task list.
        action = None
        if re.fullmatch(r"(?:please )?(?:mark|set) (?:that|it)(?: task)? (?:as )?(?:done|completed|complete|finished)", text) or text in {"complete that", "finish that", "complete it", "finish it", "i finished that", "i did that"}:
            action = "complete"
        elif text in {"delete that", "remove that", "delete it", "remove it", "delete that task", "remove that task", "delete this task"}:
            action = "delete"
        if action:
            task_id = self.focus_id
            if task_id is None:
                return "Which task do you mean? Please say its name or number."
            row = self._pending_task(task_id) if action == "complete" else self._existing_task(task_id)
            if row is None:
                self.focus_id = None
                return "That task is no longer available. Please say its name."
            if action == "complete":
                success = complete_note(task_id)
                result = f"Marked {row[1]} as completed." if success else "I couldn't complete that task."
            else:
                success = delete_note_by_id(task_id)
                result = f"Deleted {row[1]} from your tasks." if success else "I couldn't delete that task."
            self.focus_id = None
            self.last_intent = action
            return result

        if text in {
            "what about tomorrow", "and tomorrow", "what about tomorrow s tasks",
            "what about tasks tomorrow", "and for tomorrow", "tomorrow s tasks",
            "what about tomorrow then"
        }:
            if self.last_intent not in {"list", "list_tomorrow"}:
                return "Do you want me to check tomorrow's tasks?"
            tomorrow = date.today() + timedelta(days=1)
            rows = get_pending_notes(tomorrow.isoformat())
            self.last_intent = "list_tomorrow"
            self.last_date = tomorrow.isoformat()
            self.focus_id = rows[0][0] if len(rows) == 1 else None
            self.listed_ids = [r[0] for r in rows]
            if not rows:
                return "You have no pending tasks for tomorrow."
            return "Your tasks for tomorrow are: " + ". ".join(
                f"{n}. {row[1]}" for n, row in enumerate(rows, 1)
            ) + "."

        return None

    def observe(self, message, response):
        text = normalize(message)
        result = normalize(response)

        # Explicit task queries reset single-task focus if multiple records are listed.
        if any(fragment in text for fragment in (
            "my tasks", "pending tasks", "show tasks", "refresh tasks",
            "what do i need to do", "my notes for today"
        )) and not any(fragment in text for fragment in ("add ", "delete ", "mark ", "complete ")):
            rows = get_pending_notes()
            self.last_intent = "list"
            self.listed_ids = [r[0] for r in rows]
            self.focus_id = rows[0][0] if len(rows) == 1 else None
            self.last_date = date.today().isoformat()
            self.pending_description = None
            return

        # If a task was saved, derive the reference from the actual DB,
        # never assume an LLM response is proof of an action.
        if any(text.startswith(prefix) for prefix in (
            "add a task", "add a new task", "add another task",
            "make a note", "create a task", "save a task",
            "mark ", "complete ", "finish ", "delete ", "remove "
        )):
            self.pending_description = None
            if any(result.startswith(prefix) for prefix in (
                "added ", "i ve saved", "task added", "i have saved"
            )):
                # Only create a focus if the written task name uniquely
                # appears in the actual database.
                for row in get_pending_notes():
                    if _task_text(row[1]) and _task_text(row[1]) in result:
                        candidates = [
                            x for x in get_pending_notes()
                            if _task_text(x[1]) in result
                        ]
                        self.focus_id = candidates[0][0] if len(candidates) == 1 else None
                        self.last_intent = "add"
                        break
            return

        # Recognize a personal statement as a possible antecedent for "add that".
        # Don't treat arbitrary LLM claims or recommendations as task content.
        m = re.fullmatch(
            r"(?:i (?:need|want|have) to|i(?: am|'m) going to|i will) (.+)",
            message.strip().rstrip(" .!?"), flags=re.I
        )
        if m:
            description = m.group(1).strip()
            if 1 <= len(description.split()) <= 18:
                self.pending_description = description[0].upper() + description[1:]
                self.last_intent = "conversation"
                self.focus_id = None
                return
        self.pending_description = None

context = ConversationContext()
