"""A bot's conversations: separate threads of work with the same bot.

A conversation's id is its thread id: messages, the running turn, undo,
recall and approval cards are all keyed by it. Each bot's first
conversation has the bot's own id, which is where everything from before
conversations lives, so nothing had to move. That one's record is written
the first time something is stored on it; until then it is read with the
bot's old per-bot fields (its to-do list and tool selection).

Kept per conversation: the history, the to-do list and the tool selection.
Shared by all of a bot's conversations: its settings, notes, computer,
files, procedures, secrets and schedules. Since the notes are shared, a
new conversation starts with them, and a change made in one conversation
is announced in the others (the bot is told to read them again).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.services.identity import acting_for_owner, get_current_actor
from app.services.storage_service import StorageService, current_storage

logger = logging.getLogger(__name__)

ID_PREFIX = "cv-"
TITLE_MAX_CHARS = 80
AUTO_TITLE_CHARS = 60
# Conversations Cubicle opens for work that does not come from the user typing.
PURPOSE_TITLES = {"scheduled": "Scheduled"}
# Fields the client may change.
EDITABLE = {"title", "archived"}


class ConversationError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now().isoformat()


def new_id() -> str:
    return f"{ID_PREFIX}{uuid.uuid4().hex[:12]}"


def _bot(storage: StorageService, bot_id: str) -> Optional[Dict[str, Any]]:
    return next((b for b in storage.get_bots() if b.get("id") == bot_id), None)


def clean_title(text: Any) -> str:
    return " ".join(str(text or "").split())[:TITLE_MAX_CHARS]


def title_from(text: Any) -> str:
    """A title from the first thing the user wrote: its first line, cut at a word."""
    line = next((part.strip() for part in str(text or "").splitlines() if part.strip()), "")
    line = " ".join(line.split())
    if len(line) <= AUTO_TITLE_CHARS:
        return line
    cut = line[:AUTO_TITLE_CHARS].rsplit(" ", 1)[0] or line[:AUTO_TITLE_CHARS]
    return cut.rstrip(" ,.;:") + "…"


def _default_record(bot: Dict[str, Any]) -> Dict[str, Any]:
    """The bot's first conversation as it reads before it has a record of its own."""
    return {
        "id": bot["id"],
        "bot_id": bot["id"],
        "title": "",
        "created_at": bot.get("created_at") or _now(),
        "todo": list(bot.get("todo") or []),
        "tool_selection": bot.get("tool_selection"),
    }


def bot_id_of(thread_id: Optional[str], storage: Optional[StorageService] = None) -> Optional[str]:
    """The bot a conversation belongs to, or None when it is not one of this account's."""
    if not thread_id:
        return None
    storage = storage or current_storage()
    bot_ids = {b.get("id") for b in storage.get_bots()}
    if thread_id in bot_ids:
        return thread_id
    record = storage.get_conversation(thread_id)
    bot_id = record.get("bot_id") if record else None
    return bot_id if bot_id in bot_ids else None


def get(thread_id: str, storage: Optional[StorageService] = None) -> Optional[Dict[str, Any]]:
    storage = storage or current_storage()
    record = storage.get_conversation(thread_id)
    if record is not None:
        return record
    bot = _bot(storage, thread_id)
    return _default_record(bot) if bot else None


def update(thread_id: str, changes: Dict[str, Any], storage: Optional[StorageService] = None) -> Dict[str, Any]:
    storage = storage or current_storage()
    record = get(thread_id, storage)
    if record is None:
        raise ConversationError("That conversation does not exist.")
    record = {**record, **changes, "updated_at": _now()}
    storage.save_conversation(record)
    return record


def summary(record: Dict[str, Any], activity: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What the client gets for a conversation."""
    activity = activity or {}
    return {
        "id": record["id"],
        "bot_id": record["bot_id"],
        "title": record.get("title") or "",
        # first_message, generated (by a model after the first exchange), user, or fixed (Cubicle's own)
        "title_source": record.get("title_source") or ("fixed" if record.get("purpose") else None),
        "purpose": record.get("purpose"),
        "archived": bool(record.get("archived")),
        "created_at": record.get("created_at"),
        "last_activity": activity.get("last_activity") or record.get("created_at"),
        "messages": activity.get("messages", 0),
        "todo": list(record.get("todo") or []),
        "project_id": record.get("project_id"),
        # Its own thinking setting ("" follows the bot) and the level its last turn used.
        "thinking": record.get("thinking") or "",
        "thinking_last": record.get("thinking_last") or "",
        # Its own work notes, used while it is in no project.
        "work_notes": str(record.get("work_notes") or ""),
    }


def list_for_bot(bot_id: str, storage: Optional[StorageService] = None, *, everyone: bool = False) -> List[Dict[str, Any]]:
    """The acting person's conversations with the bot, most recently active first; `everyone`
    lists all of them, teammates' too (for notices and deletion, never for showing)."""
    storage = storage or current_storage()
    bot = _bot(storage, bot_id)
    if bot is None:
        raise ConversationError("Bot not found")
    records = storage.get_conversations(bot_id)
    if not any(r.get("id") == bot_id for r in records):
        records.insert(0, _default_record(bot))
    # Only the person's own: the owner's (no user_id) for the owner, a teammate's for them.
    actor = get_current_actor()
    if not everyone:
        records = [r for r in records if (r.get("user_id") or storage.owner_id) == actor]
    if not records:
        records = [create(bot_id, "", storage=storage)]  # a teammate's first conversation with a shared bot
    activity = storage.thread_activity()
    for index, record in enumerate(records):
        if not record.get("title") and (activity.get(record["id"]) or {}).get("messages"):
            # Named after what it was started with (the bot's first conversation predates titles).
            first = next((m for m in storage.get_messages(thread_id=record["id"]) if m.get("sender") == "user" and m.get("text")), None)
            if first is not None:
                records[index] = update(record["id"], {"title": title_from(first["text"]), "title_source": "first_message"}, storage)
    from app.services import conversation_titles  # here: it imports this module

    for record in records:
        if (activity.get(record["id"]) or {}).get("messages") and conversation_titles.wants_title(record):
            conversation_titles.schedule(record["id"])  # one written before titles were generated
    listed = [summary(r, activity.get(r["id"])) for r in records]
    listed.sort(key=lambda c: str(c.get("last_activity") or ""), reverse=True)
    return listed


def latest_for_bot(bot_id: str, storage: Optional[StorageService] = None) -> Dict[str, Any]:
    """The bot's most recently active conversation that is not archived (its first one when none is)."""
    storage = storage or current_storage()
    listed = [c for c in list_for_bot(bot_id, storage) if not c["archived"]]
    chosen = listed[0]["id"] if listed else bot_id
    return get(chosen, storage) or {"id": bot_id, "bot_id": bot_id}


def create(bot_id: str, title: str = "", *, purpose: Optional[str] = None, project_id: Optional[str] = None, storage: Optional[StorageService] = None) -> Dict[str, Any]:
    storage = storage or current_storage()
    bot = _bot(storage, bot_id)
    if bot is None:
        raise ConversationError("Bot not found")
    now = _now()
    record: Dict[str, Any] = {"id": new_id(), "bot_id": bot_id, "title": clean_title(title), "created_at": now, "updated_at": now, "todo": []}
    if not purpose and not acting_for_owner():
        # A teammate's conversation with a shared bot is theirs alone.
        record["user_id"] = get_current_actor()
    if purpose:
        record["purpose"] = purpose
        record["title_source"] = "fixed"
    elif record["title"]:
        record["title_source"] = "user"
    project = storage.get_project(project_id) if project_id else None
    if project_id and (project is None or project.get("bot_id") != bot_id):
        raise ConversationError("That project does not exist.")
    if project is not None:
        record["project_id"] = project["id"]
    storage.save_conversation(record)
    _seed_notes(record, bot, storage)
    if project is not None:
        from app.services.projects import work_note_message

        storage.add_message(work_note_message(record["id"], str(project.get("notes") or ""), bot_id=bot_id, by="start", project_name=project["name"]))
    return record


def _seed_notes(record: Dict[str, Any], bot: Dict[str, Any], storage: StorageService) -> None:
    """A new conversation starts with the bot's notes, which every conversation shares."""
    from app.services.bot_memory import memory_note_message

    notes = str(bot.get("memory") or "")
    if notes.strip():
        storage.add_message(memory_note_message(record["id"], notes, message_id=f"msg-{uuid.uuid4().hex[:6]}", bot_id=bot["id"], by="start"))


def for_purpose(bot_id: str, purpose: str, title: str = "", storage: Optional[StorageService] = None) -> Dict[str, Any]:
    """The bot's conversation for one kind of work (scheduled runs, an orchestrator's tasks), opened when first needed."""
    storage = storage or current_storage()
    for record in storage.get_conversations(bot_id):
        if record.get("purpose") == purpose and not record.get("archived"):
            return record
    return create(bot_id, title or PURPOSE_TITLES.get(purpose, purpose), purpose=purpose, storage=storage)


def delete(thread_id: str, storage: Optional[StorageService] = None) -> int:
    """Delete a conversation and everything in it. The bot's first one is emptied instead: it is where a bot starts."""
    storage = storage or current_storage()
    messages = storage.get_messages(thread_id=thread_id)
    deleted = storage.delete_messages(thread_id)
    storage.clear_rewinds(thread_id)
    try:
        from app.services import retrieval

        index = retrieval.index_for(storage)
        for message in messages:
            if message.get("id"):
                index.delete("message", f"{thread_id}:{message['id']}")
    except Exception as exc:  # recall is a nicety
        logger.warning("Could not drop the recall index of %s: %s", thread_id, exc)
    storage.delete_conversation(thread_id)
    return deleted


def notes_changed(bot_id: str, from_thread: Optional[str], storage: Optional[StorageService] = None) -> int:
    """Tell the bot's other conversations that its notes changed, once until they are read again.

    Each conversation only sees the notes as they were when it last read or
    wrote them; without this, a bot working from an old copy could undo
    another conversation's change with memory_write.
    """
    from app.services.bot_memory import memory_note_message

    storage = storage or current_storage()
    told = 0
    try:
        listed = list_for_bot(bot_id, storage)
    except ConversationError:
        return 0
    for item in listed:
        if item["id"] == from_thread or item["archived"] or not item["messages"]:
            continue
        record = get(item["id"], storage)
        if not record or record.get("notes_stale"):
            continue
        storage.add_message(memory_note_message(item["id"], "", message_id=f"msg-{uuid.uuid4().hex[:6]}", bot_id=bot_id, by="elsewhere"))
        update(item["id"], {"notes_stale": True}, storage)
        told += 1
    return told


def notes_seen(thread_id: Optional[str], storage: Optional[StorageService] = None) -> None:
    """The bot read or wrote its notes in this conversation: it has them as they are now."""
    if not thread_id:
        return
    storage = storage or current_storage()
    record = storage.get_conversation(thread_id)
    if record and record.get("notes_stale"):
        update(thread_id, {"notes_stale": False}, storage)
