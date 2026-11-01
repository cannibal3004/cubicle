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

Each conversation keeps its own list, on its record (services/conversations):
the work at hand is the conversation's, while the notes are the bot's.

Finished items do not pile up: when the last item is marked done the bot is
reminded to keep what is worth keeping (notes, work notes, the project log,
memory) and clear the list with todo_clear; a new plan started on a list
that is all done replaces it; and a full list makes room by dropping its
oldest done items. Open and doing items are never removed by themselves.
Ids keep counting up across clears (`todo_floor` on the conversation), so
an old id never names a new item.
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
CLEAR_ACTION = "todo.clear"
ALL_DONE_NOTE = (
    "Everything on your list is done. Before you move on, keep what is worth keeping from this job (a decision or where "
    "things stand in the work notes or project log, a lasting fact in your notes or memory), then clear the finished "
    "items with todo_clear."
)
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


def _next_id(taken, floor: int = 0) -> str:
    n = max(len(taken), floor) + 1
    while f"t{n}" in taken:
        n += 1
    return f"t{n}"


def _number(item_id: str) -> int:
    digits = str(item_id or "")[1:]
    return int(digits) if digits.isdigit() else 0


def render_todo(items: List[Dict[str, Any]]) -> str:
    """The list as the model reads it: one line per item, with its id and state."""
    if not items:
        return "(empty)"
    return "\n".join(f"- {_MARKS.get(item['state'], '[ ]')} {item['id']}: {item['text']}" for item in items)


def get_todo(thread_id: str, storage: Optional[StorageService] = None) -> List[Dict[str, Any]]:
    """The to-do list of a conversation (a bot's id names its first conversation)."""
    from app.services import conversations

    record = conversations.get(thread_id, storage or current_storage())
    try:
        return clean_todo((record or {}).get("todo") or [])
    except TodoError:
        return []


def _floor(thread_id: str, storage: StorageService) -> int:
    from app.services import conversations

    try:
        return int((conversations.get(thread_id, storage) or {}).get("todo_floor") or 0)
    except (TypeError, ValueError):
        return 0


def _save_todo(thread_id: str, items: List[Dict[str, Any]], storage: StorageService, *, cleared: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    from app.services import conversations

    if conversations.get(thread_id, storage) is None:
        raise TodoError("The conversation does not exist.")
    changes: Dict[str, Any] = {"todo": items, "todo_updated_at": datetime.now().isoformat()}
    if cleared:
        changes["todo_floor"] = max([_floor(thread_id, storage)] + [_number(i["id"]) for i in cleared])
    conversations.update(thread_id, changes, storage)
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
    storage = storage or current_storage()
    cleaned = clean_todo(items)
    kept = {item["id"] for item in cleaned}
    removed = [item for item in get_todo(bot_id, storage) if item["id"] not in kept]
    return _save_todo(bot_id, cleaned, storage, cleared=removed)


def add_todo(bot_id: str, texts: Any, storage: Optional[StorageService] = None) -> Dict[str, Any]:
    """Append one or more open items. Returns the whole list and the new ids."""
    storage = storage or current_storage()
    if isinstance(texts, str):
        texts = [texts]
    if not isinstance(texts, (list, tuple)) or not texts:
        raise TodoError("'items' must be one or more item texts.")
    current = get_todo(bot_id, storage)
    cleared: List[Dict[str, Any]] = []
    # A new plan on a list that is all done replaces it.
    if current and all(item["state"] == "done" for item in current):
        cleared, current = current, []
    floor = max([_floor(bot_id, storage)] + [_number(i["id"]) for i in cleared])
    taken = {item["id"] for item in current} | {item["id"] for item in cleared}
    added: List[str] = []
    for text in texts:
        entry = {"id": _next_id(taken, floor), "text": _clean_text(text), "state": "open"}
        taken.add(entry["id"])
        added.append(entry["id"])
        current.append(entry)
    # Full: the oldest done items make room; open and doing ones are never dropped.
    while len(current) > TODO_MAX_ITEMS:
        oldest_done = next((i for i, item in enumerate(current) if item["state"] == "done"), None)
        if oldest_done is None:
            raise TodoError(f"The to-do list is limited to {TODO_MAX_ITEMS} items, and none of the rest is done; finish or remove some first.")
        cleared.append(current.pop(oldest_done))
    result = {**_save_todo(bot_id, current, storage, cleared=cleared), "added": added}
    if cleared:
        result["cleared_done"] = len(cleared)
    return result


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
    result = {**_save_todo(bot_id, current, storage), "updated": key}
    if current[index]["state"] == "done" and all(item["state"] == "done" for item in current):
        result.update(all_done=True, note=ALL_DONE_NOTE)
    return result


def clear_done(bot_id: str, storage: Optional[StorageService] = None) -> Dict[str, Any]:
    """Remove the finished items in one go. Returns the whole list."""
    storage = storage or current_storage()
    current = get_todo(bot_id, storage)
    done = [item for item in current if item["state"] == "done"]
    left = [item for item in current if item["state"] != "done"]
    return {**_save_todo(bot_id, left, storage, cleared=done), "cleared": len(done)}


def todo_note_message(thread_id: str, items: List[Dict[str, Any]], *, message_id: str, bot_id: Optional[str] = None) -> Dict[str, Any]:
    """The thread item recording that the user edited the list."""
    return {
        "id": message_id,
        "thread_id": thread_id,
        "bot_id": bot_id or thread_id,
        "sender": "system",
        "text": render_todo(items),
        "created_at": datetime.now().isoformat(),
        "item_type": TODO_ITEM_TYPE,
        "raw_payload": {"by": "user", "items": items},
    }


def _bot_id(call: ActionInvocation) -> str:
    """The conversation whose list it is: the call's thread, else the bot's first conversation."""
    thread_id = call.arguments.get("thread_id")
    if isinstance(thread_id, str) and thread_id.strip():
        return thread_id
    bot_id = call.arguments.get("bot_id")
    if not isinstance(bot_id, str) or not bot_id.strip():
        raise TodoError("To-do actions require a bot id.")
    return bot_id


async def execute_list(call: ActionInvocation) -> Dict[str, Any]:
    return summary(get_todo(_bot_id(call)))


async def execute_add(call: ActionInvocation) -> Dict[str, Any]:
    return add_todo(_bot_id(call), call.arguments.get("items"))


async def execute_clear(call: ActionInvocation) -> Dict[str, Any]:
    return clear_done(_bot_id(call))


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
    target.register_action(
        ActionDefinition(name=CLEAR_ACTION, tool="todo", action="clear", intent="Remove the finished items of the bot's to-do list.", risk="write", requires_approval=False),
        execute_clear,
    )


register_todo_actions()
