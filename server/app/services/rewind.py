"""Undo and redo in a conversation: rewind to just before one of the user's messages.

Rewinding moves that message and everything after it out of the thread
into a per-thread stack (the `rewinds` table), so every reader of the
transcript (the turn's context, recall, search, compaction) stops seeing
them without a filter of its own. The message's text and attachments go
back to the user's composer. Redo puts the newest rewound span back, as
long as nothing has been added to the thread since; a new message from
the user clears the stack, like an editor's undo history.

Only the conversation is rewound. What the bot did in the meantime stays
done (commands run, files written, messages sent through connected apps),
so the preview lists those actions for the user to confirm. Two things are
put right instead of listed: the bot sees its notes and its to-do list in
the transcript where they last changed, so when the rewound span changed
either, a note row with the current state is added after the rewind (and
removed again by a redo). Recall vectors of rewound messages are dropped
and re-indexed on redo.

The latest compaction is a hard stop: its summary already holds what came
before it, so only messages after the compaction row can be rewound.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.services import retrieval
from app.services.context_builder import is_compaction
from app.services.storage_service import StorageService, current_storage

logger = logging.getLogger(__name__)

REWIND_BY = "rewind"
MAX_STACK = 20
NOTES_TOOLS = {"memory_write", "memory_edit"}
TODO_TOOLS = {"todo_add", "todo_update"}
# Built-in tools that only look; anything else that completed may have changed something.
READ_ONLY_TOOLS = {
    "shared_folder_list", "shared_folder_read", "computer_start", "computer_screenshot", "computer_files_list",
    "computer_read_file", "list_files", "read_file", "github_list_issues", "memory_read", "procedure_read", "todo_list", "schedule_list",
    "ask_user", "bots_list", "bots_status", "bots_reply", "connector_status", "mcp_list_servers", "mcp_test_server",
    "computer_request_takeover", "composio_search_tools",
}
# Words in an app tool's name that mark it as a read (gmail_fetch_emails, notion_search, ...).
_READ_WORDS = {"list", "get", "fetch", "read", "search", "find", "query", "describe", "status", "lookup", "retrieve", "view", "show", "check"}
_WORD = re.compile(r"[a-z0-9]+")


class RewindError(RuntimeError):
    pass


# ---- what can be rewound --------------------------------------------------------------


def editable(message: Dict[str, Any]) -> bool:
    """A message the user typed (not a schedule's or the orchestrator's)."""
    if message.get("sender") != "user" or not message.get("id"):
        return False
    payload = message.get("raw_payload") or {}
    return not payload.get("scheduled") and not payload.get("delegated")


def _after_compaction(history: List[Dict[str, Any]]) -> int:
    """Index of the first message after the latest compaction row (0 when none)."""
    for index in range(len(history) - 1, -1, -1):
        if is_compaction(history[index]):
            return index + 1
    return 0


def editable_ids(history: List[Dict[str, Any]]) -> List[str]:
    start = _after_compaction(history)
    return [str(m["id"]) for m in history[start:] if editable(m)]


def _span(history: List[Dict[str, Any]], message_id: Optional[str]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """The target message and the messages from it to the end. The newest editable message when no id is given."""
    start = _after_compaction(history)
    candidates = [i for i in range(start, len(history)) if editable(history[i])]
    if message_id:
        position = next((i for i, m in enumerate(history) if str(m.get("id")) == str(message_id)), None)
        if position is None:
            raise RewindError("That message is not in this conversation.")
        if position < start:
            raise RewindError("That message was folded into a compaction summary and can no longer be rewound.")
        if not editable(history[position]):
            raise RewindError("Only your own messages can be edited or undone.")
    else:
        if not candidates:
            raise RewindError("There is nothing to undo since the last compaction.")
        position = candidates[-1]
    return history[position], history[position:]


# ---- what the span did -------------------------------------------------------------------


def _arguments(record: Dict[str, Any]) -> Dict[str, Any]:
    args = record.get("arguments")
    if isinstance(args, dict):
        return args
    raw = record.get("raw_arguments")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _is_read(name: str) -> bool:
    if name in READ_ONLY_TOOLS:
        return True
    if name.startswith(("computer_", "shared_folder_", "memory_", "todo_", "schedule_", "bots_", "mcp_", "procedure_", "share_", "github_", "connector_")):
        return False  # a built-in that is not in the read list changes something
    words = set(_WORD.findall(name.lower()))
    return bool(words & _READ_WORDS)


def _summary(name: str, args: Dict[str, Any]) -> str:
    for key in ("command", "path", "url", "title", "name", "to", "recipient_email", "subject", "message", "text", "query"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            flat = " ".join(value.split())
            return flat if len(flat) <= 140 else flat[:137] + "…"
    if name == "computer_send_input":
        event = args.get("event") or {}
        return str(event.get("type") or "input")
    if name == "computer_install_packages":
        packages = args.get("packages")
        return ", ".join(packages) if isinstance(packages, list) else ""
    return ""


def describe_span(span: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts, the actions that stay done, and whether notes or the to-do list changed."""
    actions: List[Dict[str, str]] = []
    notes_changed = todo_changed = False
    for message in span:
        item_type = message.get("item_type")
        if message.get("sender") == "system" and item_type == "memory":
            notes_changed = True
        if message.get("sender") == "system" and item_type == "todo":
            todo_changed = True
        if message.get("sender") != "bot":
            continue
        for record in ((message.get("raw_payload") or {}).get("tool_calls") or []):
            if not isinstance(record, dict) or record.get("status") != "completed":
                continue
            name = str(record.get("name") or "")
            if name in NOTES_TOOLS:
                notes_changed = True
                continue
            if name in TODO_TOOLS:
                todo_changed = True
                continue
            if not name or _is_read(name):
                continue
            actions.append({"tool": name, "summary": _summary(name, _arguments(record))})
    visible = [m for m in span if m.get("sender") in ("user", "bot")]
    return {"messages": len(visible), "rows": len(span), "actions": actions, "notes_changed": notes_changed, "todo_changed": todo_changed}


def preview(thread_id: str, message_id: Optional[str] = None, storage: Optional[StorageService] = None) -> Dict[str, Any]:
    storage = storage or current_storage()
    target, span = _span(storage.get_messages(thread_id=thread_id), message_id)
    return {"message_id": target["id"], **describe_span(span)}


# ---- rewind and redo ----------------------------------------------------------------------


def _bot(storage: StorageService, bot_id: str) -> Dict[str, Any]:
    return next((b for b in storage.get_bots() if b.get("id") == bot_id), {}) or {}


def _resync_rows(thread_id: str, info: Dict[str, Any], storage: StorageService) -> List[Dict[str, Any]]:
    """Note rows showing the bot its current notes and to-do list after the rewind."""
    from app.services.bot_memory import memory_note_message
    from app.services.bot_todo import todo_note_message

    bot = _bot(storage, thread_id)
    rows: List[Dict[str, Any]] = []
    if info["notes_changed"]:
        row = memory_note_message(thread_id, str(bot.get("memory") or ""), message_id=f"msg-{uuid.uuid4().hex[:6]}")
        row["raw_payload"]["by"] = REWIND_BY
        rows.append(row)
    if info["todo_changed"]:
        row = todo_note_message(thread_id, list(bot.get("todo") or []), message_id=f"msg-{uuid.uuid4().hex[:6]}")
        row["raw_payload"]["by"] = REWIND_BY
        rows.append(row)
    return rows


def _unindex(thread_id: str, span: List[Dict[str, Any]]) -> None:
    try:
        index = retrieval.index_for()
        for message in span:
            if message.get("id"):
                index.delete("message", f"{thread_id}:{message['id']}")
    except Exception as exc:  # recall is a nicety
        logger.warning("Could not drop recall vectors after a rewind of %s: %s", thread_id, exc)


def rewind(thread_id: str, message_id: Optional[str] = None, storage: Optional[StorageService] = None) -> Dict[str, Any]:
    """Move the message and everything after it to the redo stack. Returns the rewound message and what was done."""
    storage = storage or current_storage()
    target, span = _span(storage.get_messages(thread_id=thread_id), message_id)
    info = describe_span(span)
    storage.delete_message_ids(thread_id, [str(m["id"]) for m in span])
    resync = _resync_rows(thread_id, info, storage)
    for row in resync:
        storage.add_message(row)
    remaining = storage.get_messages(thread_id=thread_id)
    entry = {
        "id": f"rw-{uuid.uuid4().hex[:8]}",
        "created_at": datetime.now().isoformat(),
        "messages": span,
        "resync_ids": [row["id"] for row in resync],
        "anchor": str(remaining[-1]["id"]) if remaining else "",
    }
    storage.push_rewind(thread_id, entry, keep=MAX_STACK)
    _unindex(thread_id, span)
    return {
        "message": target,
        "removed": info["messages"],
        "actions": info["actions"],
        "resynced": {"notes": info["notes_changed"], "todo": info["todo_changed"]},
        "redo": redo_state(thread_id, storage),
    }


def redo_state(thread_id: str, storage: Optional[StorageService] = None) -> Dict[str, Any]:
    """Whether a redo is possible now, and how many messages it would bring back."""
    storage = storage or current_storage()
    entry = storage.peek_rewind(thread_id)
    if not entry:
        return {"available": False, "messages": 0, "depth": 0}
    if not _anchor_holds(thread_id, entry, storage):
        storage.clear_rewinds(thread_id)  # the thread moved on: the redo history is gone
        return {"available": False, "messages": 0, "depth": 0}
    visible = [m for m in entry.get("messages") or [] if m.get("sender") in ("user", "bot")]
    return {"available": True, "messages": len(visible), "depth": storage.count_rewinds(thread_id)}


def _anchor_holds(thread_id: str, entry: Dict[str, Any], storage: StorageService) -> bool:
    remaining = storage.get_messages(thread_id=thread_id)
    last = str(remaining[-1]["id"]) if remaining else ""
    return last == str(entry.get("anchor") or "")


def redo(thread_id: str, storage: Optional[StorageService] = None) -> Dict[str, Any]:
    """Put the newest rewound span back. Raises RewindError when there is none or the thread moved on."""
    storage = storage or current_storage()
    entry = storage.peek_rewind(thread_id)
    if not entry:
        raise RewindError("There is nothing to redo.")
    if not _anchor_holds(thread_id, entry, storage):
        storage.clear_rewinds(thread_id)
        raise RewindError("The conversation has moved on since the undo, so it cannot be redone.")
    storage.pop_rewind(thread_id)
    resync = [str(i) for i in entry.get("resync_ids") or []]
    if resync:
        storage.delete_message_ids(thread_id, resync)
    messages = [m for m in entry.get("messages") or [] if isinstance(m, dict)]
    for message in messages:
        storage.add_message(message)
    from app.services import recall

    for message in messages:
        recall.schedule_index(thread_id, message)
    visible = [m for m in messages if m.get("sender") in ("user", "bot")]
    return {"restored": len(visible), "redo": redo_state(thread_id, storage)}


def forget(thread_id: str, storage: Optional[StorageService] = None) -> None:
    """A new message from the user: the redo history no longer applies."""
    try:
        (storage or current_storage()).clear_rewinds(thread_id)
    except Exception as exc:
        logger.warning("Could not clear the redo history of %s: %s", thread_id, exc)
