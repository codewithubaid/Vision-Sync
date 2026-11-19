"""
VisionSync tool layer (compatible with the current notes/reminder/memory managers).

LLMs may propose a tool name and arguments. This module only executes allow-listed
Python functions; it never evaluates model-generated code. Database IDs, not
display-list positions, are required for destructive actions.
"""

from datetime import date, datetime
from typing import Any, List, Optional, get_type_hints, get_origin, get_args
import inspect
import re

from notes import (
    add_note, get_all_notes, get_pending_notes, complete_note,
    reopen_note, delete_note_by_id,
)
from reminder_manager import (
    add_reminder as _add_reminder,
    get_reminder as _get_reminder,
    list_reminders as _list_reminders,
    delete_reminder as _delete_reminder,
)
from memory import remember, list_memories, search_memories, forget_memory, forget_memory_by_text


def _clean_text(value: Any, field: str, limit: int = 500) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text.")
    value = " ".join(value.split()).strip()
    if not value or len(value) > limit:
        raise ValueError(f"{field} must contain 1 to {limit} characters.")
    return value


def create_task(title: str, task_date: Optional[str] = None) -> dict:
    title = _clean_text(title, "Task title")
    if task_date is not None:
        date.fromisoformat(task_date)
    before = {int(row[0]) for row in get_all_notes(task_date)}
    add_note(title, task_date)
    rows = get_all_notes(task_date)
    created = [row for row in rows if int(row[0]) not in before and row[1] == title]
    if not created:
        # Some managers return no ID and sort/filter rows differently; verify by
        # exact text and newest row without claiming success if absent.
        created = [row for row in rows if row[1] == title]
    if not created:
        raise RuntimeError("Task creation could not be verified in SQLite.")
    row = max(created, key=lambda item: int(item[0]))
    return {"success": True, "id": int(row[0]), "title": row[1],
            "date": row[2], "status": row[4] if len(row) > 4 else "pending"}


def list_tasks(task_date: Optional[str] = None, pending_only: bool = True) -> List[dict]:
    if task_date is not None:
        date.fromisoformat(task_date)
    rows = get_pending_notes(task_date) if pending_only else get_all_notes(task_date)
    return [
        {"id": int(r[0]), "title": r[1], "date": r[2],
         "created_at": r[3] if len(r) > 3 else None,
         "status": r[4] if len(r) > 4 else "pending"}
        for r in rows
    ]


def complete_task(task_id: int) -> dict:
    task_id = int(task_id)
    if not complete_note(task_id):
        raise ValueError("Task not found or already completed.")
    return {"success": True, "id": task_id, "status": "completed"}


def reopen_task(task_id: int) -> dict:
    task_id = int(task_id)
    if not reopen_note(task_id):
        raise ValueError("Task not found or already pending.")
    return {"success": True, "id": task_id, "status": "pending"}


def delete_task(task_id: int) -> dict:
    task_id = int(task_id)
    if not delete_note_by_id(task_id):
        raise ValueError("Task not found.")
    return {"success": True, "id": task_id, "deleted": True}


def create_reminder(title: str, due_at: str) -> dict:
    title = _clean_text(title, "Reminder title")
    due = datetime.fromisoformat(due_at)
    saved = _add_reminder(title, due)
    if not saved or not saved.get("id"):
        raise RuntimeError("Reminder manager did not return a saved reminder.")
    verified = _get_reminder(saved["id"])
    if not verified or verified.get("title") != title:
        raise RuntimeError("Reminder creation could not be verified in SQLite.")
    return {"success": True, **verified}


def list_reminders() -> List[dict]:
    return _list_reminders()


def delete_reminder(reminder_id: int) -> dict:
    reminder_id = int(reminder_id)
    if not _delete_reminder(reminder_id):
        raise ValueError("Reminder not found.")
    return {"success": True, "id": reminder_id, "deleted": True}


def delete_reminders(reminder_ids: List[int]) -> dict:
    if not isinstance(reminder_ids, list) or not reminder_ids:
        raise ValueError("Provide at least one reminder database ID.")
    ids = list(dict.fromkeys(int(x) for x in reminder_ids))
    existing = {int(r["id"]) for r in _list_reminders()}
    missing = [ident for ident in ids if ident not in existing]
    if missing:
        raise ValueError(f"Reminder IDs not found or not pending: {missing}")
    deleted = []
    for ident in ids:
        if not _delete_reminder(ident):
            raise RuntimeError(f"Reminder {ident} could not be deleted.")
        deleted.append(ident)
    return {"success": True, "deleted_ids": deleted, "count": len(deleted)}


def save_personal_memory(information: str, category: str = "personal",
                         key: Optional[str] = None) -> dict:
    information = _clean_text(information, "Memory", 1000)

    memory_id = remember(information, category=category, key=key)
    row = next((x for x in list_memories(200) if int(x["id"]) == int(memory_id)), None)
    if row is None:
        raise RuntimeError("Memory save could not be verified in SQLite.")
    return {"success": True, **row}


def retrieve_personal_memory(query: str, limit: int = 8) -> List[dict]:
    query = _clean_text(query, "Memory query", 300)
    return search_memories(query, limit=max(1, min(int(limit), 20)))


def list_personal_memories(limit: int = 50) -> List[dict]:
    return list_memories(max(1, min(int(limit), 200)))


def delete_personal_memory(memory_id: int) -> dict:
    """Legacy internal-ID deletion; IDs are not intended for user display."""
    memory_id = int(memory_id)
    if not forget_memory(memory_id):
        raise ValueError("Memory record not found.")
    if any(int(row["id"]) == memory_id for row in list_memories(200)):
        raise RuntimeError("Memory deletion could not be verified in SQLite.")
    return {"success": True, "id": memory_id, "deleted": True}


def delete_personal_memory_by_text(description: str) -> dict:
    """Delete one unambiguous memory by its saved wording or key."""
    description = _clean_text(description, "Memory description", 300)
    result = forget_memory_by_text(description)
    if not result.get("success"):
        raise ValueError(result.get("error", "Memory could not be deleted."))
    return result


TOOL_FUNCTIONS = {
    "create_task": create_task, "list_tasks": list_tasks,
    "complete_task": complete_task, "reopen_task": reopen_task,
    "delete_task": delete_task, "create_reminder": create_reminder,
    "list_reminders": list_reminders, "delete_reminder": delete_reminder,
    "delete_reminders": delete_reminders,
    "save_personal_memory": save_personal_memory,
    "retrieve_personal_memory": retrieve_personal_memory,
    "list_personal_memories": list_personal_memories,
    "delete_personal_memory": delete_personal_memory,
    "delete_personal_memory_by_text": delete_personal_memory_by_text,
}

# Human-readable descriptions. Parameter schemas are generated from the real
# Python signatures below, so optional/default arguments cannot drift.
TOOL_DESCRIPTIONS = {
    "create_task": "Save a task with an optional date (YYYY-MM-DD).",
    "list_tasks": "List tasks, optionally for a date; pending_only defaults to true.",
    "complete_task": "Mark a task completed using its database ID.",
    "reopen_task": "Reopen a completed task using its database ID.",
    "delete_task": "Delete a task using its database ID.",
    "create_reminder": "Create a reminder at a local ISO datetime (YYYY-MM-DDTHH:MM).",
    "list_reminders": "List pending reminders. This tool takes no arguments.",
    "delete_reminder": "Delete one reminder using its database ID.",
    "delete_reminders": "Delete multiple reminders using database IDs, never display positions.",
    "save_personal_memory": "Save or correct a personal fact. For mother name, use key profilemothername and store just the name (e.g. Teena).",
    "retrieve_personal_memory": "Search saved personal memories using a natural-language query.",
    "list_personal_memories": "List saved personal memories.",
    "delete_personal_memory": "Legacy internal operation. Prefer deleting memories by their saved wording, not database IDs.",
    "delete_personal_memory_by_text": "Delete one unambiguous saved memory by describing its content. Never ask the user for a database ID.",
}


def _annotation_schema(annotation):
    """Convert a Python annotation to a JSON Schema fragment."""
    if annotation in (str,):
        return {"type": "string"}
    if annotation in (int,):
        return {"type": "integer"}
    if annotation in (float,):
        return {"type": "number"}
    if annotation in (bool,):
        return {"type": "boolean"}

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is list:
        item_type = args[0] if args else str
        return {"type": "array", "items": _annotation_schema(item_type)}

    # Optional[T] / T | None
    if origin is not None and type(None) in args:
        non_none = [arg for arg in args if arg is not type(None)]
        base = _annotation_schema(non_none[0]) if non_none else {"type": "string"}
        base["nullable"] = True
        return base

    return {"type": "string"}


def _build_tool_definitions():
    definitions = []
    for name, function in TOOL_FUNCTIONS.items():
        signature = inspect.signature(function)
        try:
            hints = get_type_hints(function)
        except Exception:
            hints = {}
        properties = {}
        required = []
        for parameter_name, parameter in signature.parameters.items():
            annotation = hints.get(parameter_name, parameter.annotation)
            if annotation is inspect.Parameter.empty:
                annotation = str
            schema = _annotation_schema(annotation)
            schema["description"] = parameter_name.replace("_", " ")
            properties[parameter_name] = schema
            if parameter.default is inspect.Parameter.empty:
                required.append(parameter_name)

        definitions.append({
            "name": name,
            "description": TOOL_DESCRIPTIONS.get(name, function.__doc__ or name),
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        })
    return definitions


TOOL_DEFINITIONS = _build_tool_definitions()


def execute_tool(name: str, arguments: Optional[dict] = None) -> dict:
    """Execute one allow-listed tool after strict signature validation."""
    if name not in TOOL_FUNCTIONS:
        return {"success": False, "error": f"Unknown VisionSync tool: {name}"}
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return {"success": False, "error": "Tool arguments must be a JSON object."}

    function = TOOL_FUNCTIONS[name]
    try:
        signature = inspect.signature(function)
        # bind() rejects unknown keys and missing required parameters while
        # accepting omitted optional parameters with their Python defaults.
        bound = signature.bind(**arguments)
        bound.apply_defaults()
        result = function(**bound.arguments)
        return result if isinstance(result, dict) else {"success": True, "result": result}
    except TypeError as exc:
        return {
            "success": False,
            "error": f"Invalid arguments for {name}: {exc}",
            "expected_parameters": list(inspect.signature(function).parameters),
        }
    except (ValueError, RuntimeError) as exc:
        return {"success": False, "error": str(exc)}
