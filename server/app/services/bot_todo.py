"""A bot's to-do list: the working list it keeps while on a job.

Notes (bot_memory) are for what stays true: the goal, decisions, where
things are. The to-do list is for what is left to do right now. It is a
list of short items with a state (open, doing, done), each with a short id
so a change costs one small call rather than a rewrite of the list.

It reaches the model the same way the notes do, without touching the
system prompt or the provider's prompt cache: every todo tool result
carries the whole list, so the newest list is in the transcript where it
last changed; a user edit from the chat is recorded as a note in the
thread; and each compaction stores a snapshot that the summary renders. So
a turn that was cut off, or a compaction that folded the older rounds
away, does not lose what was done and what remains.

The list lives on the bot record (`todo`) like the notes do.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from app.config import settings
from app.services.action_gateway import ActionDefinition, ActionGateway, ActionInvocation, action_gateway
from app.services.storage_service import StorageService, current_storage

TODO_MAX_ITEMS = settings.BOT_TODO_MAX_ITEMS
TODO_ITEM_MAX_CHARS = 300
TODO_ITEM_TYPE = "todo"
STATES = ("open", "doing", "done")
LIST_ACTION = "todo.list"
ADD_ACTION = "todo.add"
UPDATE_ACTION = "todo.update"
_MARKS = {"open": "[ ]", "doing": "[~]", "done": "[x]"}


class TodoError(RuntimeError):
    pass


def _clean_text(text: Any) -> str:
    if not isinstance(text, str):
        raise TodoError("A to-do item must be text.")
    cleaned = " ".join(text.split())
    if not cleaned:
        raise TodoError("A to-do item cannot be empty.")
    if len(cleaned) > TODO_ITEM_MAX_CHARS:
        raise TodoError(f"A to-do item is limited to {TODO_ITEM_MAX_CHARS} characters; this one is {len(cleaned)}.")
    return cleaned


def _clean_state(state: Any) -> str:
    value = str(state or "open").strip().lower()
    if value not in STATES:
        raise TodoError("A to-do item's state must be open, doing or done.")
    return value


def clean_todo(items: Any) -> List[Dict[str, Any]]:
    """A valid list of {id, text, state}. Ids are renumbered when missing or duplicated."""
    if items is None:
        return []
    if not isinstance(items, (list, tuple)):
        raise TodoError("The to-do list must be a list of items.")
    if len(items) > TODO_MAX_ITEMS:
        raise TodoError(f"The to-do list is limited to {TODO_MAX_ITEMS} items; this has {len(items)}.")
    cleaned: List[Dict[str, Any]] = []
    seen = set()
    for item in items:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            raise TodoError("Each to-do item must be an object with text and state.")
        entry = {"id": str(item.get("id") or "").strip(), "text": _clean_text(item.get("text")), "state": _clean_state(item.get("state"))}
        if not entry["id"] or entry["id"] in seen:
            entry["id"] = _next_id(seen)
        seen.add(entry["id"])
        cleaned.append(entry)
    return cleaned


def _next_id(taken) -> str:
    n = len(taken) + 1
    while f"t{n}" in taken:
        n += 1
    return f"t{n}"


def render_todo(items: List[Dict[str, Any]]) -> str:
    """The list as the model reads it: one line per item, with its id and state."""
    if not items:
        return "(empty)"
    return "\n".join(f"- {_MARKS.get(item['state'], '[ ]')} {item['id']}: {item['text']}" for item in items)


def get_todo(bot_id: str, storage: Optional[StorageService] = None) -> List[Dict[str, Any]]:
    for bot in (storage or current_storage()).get_bots():
        if bot.get("id") == bot_id:
            try:
                return clean_todo(bot.get("todo") or [])
            except TodoError:
                return []
    return []


def _save_todo(bot_id: str, items: List[Dict[str, Any]], storage: StorageService) -> Dict[str, Any]:
    bots = storage.get_bots()
    bot = next((b for b in bots if b.get("id") == bot_id), None)
    if bot is None:
        raise TodoError("The bot does not exist.")
    bot["todo"] = items
    bot["todo_updated_at"] = datetime.now().isoformat()
    storage.save_bots(bots)
    return summary(items)


def summary(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "items": items,
        "open": sum(1 for i in items if i["state"] != "done"),
        "done": sum(1 for i in items if i["state"] == "done"),
        "limit": TODO_MAX_ITEMS,
    }


def set_todo(bot_id: str, items: Any, storage: Optional[StorageService] = None) -> Dict[str, Any]:
    """Replace the list (the user's edits from the chat). Returns the stored list."""
    return _save_todo(bot_id, clean_todo(items), storage or current_storage())


def add_todo(bot_id: str, texts: Any, storage: Optional[StorageService] = None) -> Dict[str, Any]:
    """Append one or more open items. Returns the whole list and the new ids."""
    storage = storage or current_storage()
    if isinstance(texts, str):
        texts = [texts]
    if not isinstance(texts, (list, tuple)) or not texts:
        raise TodoError("'items' must be one or more item texts.")
    current = get_todo(bot_id, storage)
    taken = {item["id"] for item in current}
    added: List[str] = []
    for text in texts:
        entry = {"id": _next_id(taken), "text": _clean_text(text), "state": "open"}
        taken.add(entry["id"])
        added.append(entry["id"])
        current.append(entry)
    if len(current) > TODO_MAX_ITEMS:
        raise TodoError(f"The to-do list is limited to {TODO_MAX_ITEMS} items; remove finished ones first.")
    return {**_save_todo(bot_id, current, storage), "added": added}


def update_todo(
    bot_id: str, item_id: Any, *, state: Any = None, text: Any = None, remove: bool = False, storage: Optional[StorageService] = None
) -> Dict[str, Any]:
    """Change one item's state or text, or remove it. Returns the whole list."""
    storage = storage or current_storage()
    current = get_todo(bot_id, storage)
    key = str(item_id or "").strip()
    index = next((i for i, item in enumerate(current) if item["id"] == key), None)
    if index is None:
        raise TodoError(f"There is no to-do item {key!r}; todo_list shows the current ids.")
    if remove:
        current.pop(index)
        return {**_save_todo(bot_id, current, storage), "removed": key}
    if state is None and text is None:
        raise TodoError("Give a new state, a new text, or remove=true.")
    if state is not None:
        current[index]["state"] = _clean_state(state)
    if text is not None:
        current[index]["text"] = _clean_text(text)
    return {**_save_todo(bot_id, current, storage), "updated": key}


def todo_note_message(bot_id: str, items: List[Dict[str, Any]], *, message_id: str) -> Dict[str, Any]:
    """The thread item recording that the user edited the list."""
    return {
        "id": message_id,
        "thread_id": bot_id,
        "bot_id": bot_id,
        "sender": "system",
        "text": render_todo(items),
        "created_at": datetime.now().isoformat(),
        "item_type": TODO_ITEM_TYPE,
        "raw_payload": {"by": "user", "items": items},
    }


def _bot_id(call: ActionInvocation) -> str:
    bot_id = call.arguments.get("bot_id")
    if not isinstance(bot_id, str) or not bot_id.strip():
        raise TodoError("To-do actions require a bot id.")
    return bot_id


async def execute_list(call: ActionInvocation) -> Dict[str, Any]:
    return summary(get_todo(_bot_id(call)))


async def execute_add(call: ActionInvocation) -> Dict[str, Any]:
    return add_todo(_bot_id(call), call.arguments.get("items"))


async def execute_update(call: ActionInvocation) -> Dict[str, Any]:
    return update_todo(
        _bot_id(call),
        call.arguments.get("id"),
        state=call.arguments.get("state"),
        text=call.arguments.get("text"),
        remove=bool(call.arguments.get("remove")),
    )


def register_todo_actions(gateway: Optional[ActionGateway] = None) -> None:
    target = gateway or action_gateway
    target.register_action(
        ActionDefinition(name=LIST_ACTION, tool="todo", action="list", intent="Show the bot its to-do list.", risk="read", requires_approval=False),
        execute_list,
    )
    target.register_action(
        ActionDefinition(name=ADD_ACTION, tool="todo", action="add", intent="Add items to the bot's to-do list.", risk="write", requires_approval=False),
        execute_add,
    )
    target.register_action(
        ActionDefinition(
            name=UPDATE_ACTION, tool="todo", action="update", intent="Change or remove one item of the bot's to-do list.", risk="write", requires_approval=False
        ),
        execute_update,
    )


register_todo_actions()
