"""A bot's notes: one small document it keeps across turns and compactions.

The notes live on the bot record (`memory`), so they survive anything that
happens to the thread. They reach the model without disturbing the
provider's prompt cache: the system prompt never carries them. Instead the
bot's own memory_write calls stay in the append-only transcript (the result
echoes the full notes), a user edit from the bot editor is recorded as a
note in the thread, and each compaction stores a snapshot that the summary
message renders. So at any point the newest notes are visible in the
context exactly once, at the position where they changed.

memory_edit replaces one unique passage, the way an editor's find-and-replace
does. Its result carries only the changed passage and the new size, not the
whole document: the model has the last full copy (read, write or compaction
snapshot) plus every edit since, which is enough to know the current text,
and it keeps a change of one line from costing a full rewrite in output
tokens and in context.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from app.config import settings
from app.services.action_gateway import ActionDefinition, ActionGateway, ActionInvocation, action_gateway
from app.services.storage_service import StorageService, current_storage

MEMORY_MAX_CHARS = settings.BOT_MEMORY_MAX_CHARS
MEMORY_ITEM_TYPE = "memory"
READ_ACTION = "memory.read"
WRITE_ACTION = "memory.write"
EDIT_ACTION = "memory.edit"
# How much of the notes around an edit the result shows.
EXCERPT_CONTEXT = 120


class MemoryError(RuntimeError):
    pass


def clean_memory(text: Any) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        raise MemoryError("Notes must be text.")
    cleaned = text.replace("\r\n", "\n").strip()
    if len(cleaned) > MEMORY_MAX_CHARS:
        raise MemoryError(f"Notes are limited to {MEMORY_MAX_CHARS} characters; these are {len(cleaned)}.")
    return cleaned


def get_memory(bot_id: str, storage: Optional[StorageService] = None) -> str:
    for bot in (storage or current_storage()).get_bots():
        if bot.get("id") == bot_id:
            return str(bot.get("memory") or "")
    return ""


def set_memory(bot_id: str, text: Any, storage: Optional[StorageService] = None) -> Dict[str, Any]:
    """Replace the bot's notes. Returns the stored notes and when."""
    storage = storage or current_storage()
    cleaned = clean_memory(text)
    return {"memory": cleaned, **_save_memory(bot_id, cleaned, storage)}


def _save_memory(bot_id: str, cleaned: str, storage: StorageService) -> Dict[str, Any]:
    bots = storage.get_bots()
    bot = next((b for b in bots if b.get("id") == bot_id), None)
    if bot is None:
        raise MemoryError("The bot does not exist.")
    bot["memory"] = cleaned
    bot["memory_updated_at"] = datetime.now().isoformat()
    storage.save_bots(bots)
    return {"chars": len(cleaned), "updated_at": bot["memory_updated_at"]}


def _excerpt(text: str, start: int, end: int) -> str:
    """The lines holding text[start:end], plus one neighbouring line on each side (trimmed)."""
    lo = text.rfind("\n", 0, start) + 1
    hi = text.find("\n", end)
    if hi == -1:
        hi = len(text)
    if lo > 0:
        previous = text.rfind("\n", 0, lo - 1) + 1
        lo = max(previous, lo - 1 - EXCERPT_CONTEXT)
    if hi < len(text):
        following = text.find("\n", hi + 1)
        if following == -1:
            following = len(text)
        hi = min(following, hi + 1 + EXCERPT_CONTEXT)
    prefix = "..." if lo > 0 else ""
    suffix = "..." if hi < len(text) else ""
    return f"{prefix}{text[lo:hi]}{suffix}"


def edit_memory(bot_id: str, old_text: Any, new_text: Any, storage: Optional[StorageService] = None) -> Dict[str, Any]:
    """Replace one unique passage of the bot's notes. Returns the new size and the changed passage in context."""
    storage = storage or current_storage()
    if not isinstance(old_text, str) or not old_text.strip():
        raise MemoryError("'old_text' must be the exact passage to replace.")
    if new_text is None:
        new_text = ""
    if not isinstance(new_text, str):
        raise MemoryError("'new_text' must be text.")
    old_text = old_text.replace("\r\n", "\n")
    new_text = new_text.replace("\r\n", "\n")
    current = get_memory(bot_id, storage)
    if not current:
        raise MemoryError("The notes are empty; write them with memory_write.")
    count = current.count(old_text)
    if count == 0:
        raise MemoryError(
            "The passage to replace is not in the notes. Copy it exactly (memory_read shows the current notes) or rewrite with memory_write."
        )
    if count > 1:
        raise MemoryError(f"The passage to replace appears {count} times; include more of the surrounding text so it matches once.")
    start = current.index(old_text)
    before = current[:start] + new_text
    updated = clean_memory(before + current[start + len(old_text):])
    saved = _save_memory(bot_id, updated, storage)
    # clean_memory only strips the ends of the document, so the passage sits
    # at `start` less whatever leading whitespace went away.
    at = max(0, start - (len(before) - len(before.lstrip())))
    return {**saved, "excerpt": _excerpt(updated, at, min(len(updated), at + len(new_text)))}


def memory_note_message(bot_id: str, text: str, *, message_id: str) -> Dict[str, Any]:
    """The thread item recording that the user edited the notes."""
    return {
        "id": message_id,
        "thread_id": bot_id,
        "bot_id": bot_id,
        "sender": "system",
        "text": text,
        "created_at": datetime.now().isoformat(),
        "item_type": MEMORY_ITEM_TYPE,
        "raw_payload": {"by": "user", "chars": len(text)},
    }


def _bot_id(call: ActionInvocation) -> str:
    bot_id = call.arguments.get("bot_id")
    if not isinstance(bot_id, str) or not bot_id.strip():
        raise MemoryError("Memory actions require a bot id.")
    return bot_id


async def execute_read(call: ActionInvocation) -> Dict[str, Any]:
    memory = get_memory(_bot_id(call))
    return {"memory": memory, "chars": len(memory), "limit": MEMORY_MAX_CHARS}


async def execute_write(call: ActionInvocation) -> Dict[str, Any]:
    result = set_memory(_bot_id(call), call.arguments.get("text"))
    return {**result, "limit": MEMORY_MAX_CHARS}


async def execute_edit(call: ActionInvocation) -> Dict[str, Any]:
    result = edit_memory(_bot_id(call), call.arguments.get("old_text"), call.arguments.get("new_text"))
    return {**result, "limit": MEMORY_MAX_CHARS}


def register_memory_actions(gateway: Optional[ActionGateway] = None) -> None:
    target = gateway or action_gateway
    target.register_action(
        ActionDefinition(
            name=READ_ACTION,
            tool="memory",
            action="read",
            intent="Show the bot its own notes.",
            risk="read",
            requires_approval=False,
        ),
        execute_read,
    )
    target.register_action(
        ActionDefinition(
            name=WRITE_ACTION,
            tool="memory",
            action="write",
            intent="Replace the bot's own notes.",
            risk="write",
            requires_approval=False,
        ),
        execute_write,
    )
    target.register_action(
        ActionDefinition(
            name=EDIT_ACTION,
            tool="memory",
            action="edit",
            intent="Change one passage of the bot's own notes.",
            risk="write",
            requires_approval=False,
        ),
        execute_edit,
    )


register_memory_actions()
