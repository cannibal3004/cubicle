"""A bot's notes: one small document it keeps across turns and compactions.

The notes live on the bot record (`memory`), so they survive anything that
happens to the thread. They reach the model without disturbing the
provider's prompt cache: the system prompt never carries them. Instead the
bot's own memory_write calls stay in the append-only transcript (the result
echoes the full notes), a user edit from the bot editor is recorded as a
note in the thread, and each compaction stores a snapshot that the summary
message renders. So at any point the newest notes are visible in the
context exactly once, at the position where they changed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from app.services.action_gateway import ActionDefinition, ActionGateway, ActionInvocation, action_gateway
from app.services.storage_service import StorageService, storage_service

MEMORY_MAX_CHARS = 6000
MEMORY_ITEM_TYPE = "memory"
READ_ACTION = "memory.read"
WRITE_ACTION = "memory.write"


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
    for bot in (storage or storage_service).get_bots():
        if bot.get("id") == bot_id:
            return str(bot.get("memory") or "")
    return ""


def set_memory(bot_id: str, text: Any, storage: Optional[StorageService] = None) -> Dict[str, Any]:
    """Replace the bot's notes. Returns the stored notes and when."""
    storage = storage or storage_service
    cleaned = clean_memory(text)
    bots = storage.get_bots()
    bot = next((b for b in bots if b.get("id") == bot_id), None)
    if bot is None:
        raise MemoryError("The bot does not exist.")
    bot["memory"] = cleaned
    bot["memory_updated_at"] = datetime.now().isoformat()
    storage.save_bots(bots)
    return {"memory": cleaned, "chars": len(cleaned), "updated_at": bot["memory_updated_at"]}


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


register_memory_actions()
