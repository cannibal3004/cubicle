"""Build the model's context from the stored transcript.

The rule that matters for speed: the context is append-only. A message is
sent exactly as it was sent the turn before, so the provider's prompt cache
resumes from the end of the previous turn and a turn costs only the tokens
it adds. Tool results are bounded when they are stored (trimmed per result,
capped per reply) and then replayed in full, in the provider's native shape
(assistant tool_calls, tool results, final text), for every bot turn. When
the thread grows past the window, compaction (context_compactor) folds the
older part into one stored summary and the context starts there.
"""

import json
from typing import Any, Dict, List, Optional

from app.config import settings
from app.services.attachments import for_model as attachment_for_model
from app.services.tool_names import current_name

COMPACTION_ITEM_TYPE = "compaction"
# A user edit of the bot's notes, recorded in the thread where it happened.
MEMORY_ITEM_TYPE = "memory"
SUMMARY_LEAD = "Summary of the conversation so far (earlier messages were compacted):"
MEMORY_LEAD = "Your notes (memory) at this point:"
MEMORY_EDIT_LEAD = "The user edited your notes (memory). They now read:"
# A user edit of the bot's to-do list, recorded in the thread where it happened.
TODO_ITEM_TYPE = "todo"
TODO_LEAD = "Your to-do list at this point:"
TODO_EDIT_LEAD = "The user edited your to-do list. It is now:"
# What Cubicle looked up for a user message (attachment passages, procedures,
# recalled exchanges). Stored with the message and replayed the same on every
# later turn, so the prompt's beginning never changes and the model server's
# prompt cache holds.
TURN_CONTEXT_LEAD = "[Cubicle looked this up for the message above. It is reference material, not part of what the user wrote.]"
# After the user rewound the conversation past a change: the state as it stands.
REWIND_NOTES_LEAD = "The user rewound the conversation; messages after this point were removed. Your notes (memory) as they stand now:"
REWIND_TODO_LEAD = "The user rewound the conversation; messages after this point were removed. Your to-do list as it stands now:"
# A user message a schedule posted; the model is told so it reports rather than converses.
SCHEDULED_LEAD = "[Scheduled task '{name}', run automatically at {at}. Nobody is reading live: do the job and reply with a report.]"
# A task handed over by the orchestrator bot: the reply goes back to it.
DELEGATED_LEAD = "[Task from {name}, the orchestrator bot, on the user's behalf. Do it as you would for the user; your reply goes back to {name}, who reports to the user.]"


def is_compaction(message: Dict[str, Any]) -> bool:
    return message.get("sender") == "system" and message.get("item_type") == COMPACTION_ITEM_TYPE


def is_memory_note(message: Dict[str, Any]) -> bool:
    return message.get("sender") == "system" and message.get("item_type") == MEMORY_ITEM_TYPE


def is_todo_note(message: Dict[str, Any]) -> bool:
    return message.get("sender") == "system" and message.get("item_type") == TODO_ITEM_TYPE


def _in_context(message: Dict[str, Any]) -> bool:
    return message.get("sender") in ("user", "bot") or is_memory_note(message) or is_todo_note(message)


def latest_compaction(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for message in reversed(history):
        if is_compaction(message):
            return message
    return None


def live_messages(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The messages the latest compaction does not cover, in stored order.

    Messages are stored in insertion order and a compaction is inserted after
    the newest user message (the one that started the turn), so the live
    range starts after the message the compaction says it covers, not after
    the compaction row. Compaction rows and other system rows are left out.
    """
    compaction = latest_compaction(history)
    if compaction is None:
        return [m for m in history if _in_context(m)]
    covers = (compaction.get("raw_payload") or {}).get("covers_through")
    start = next((i for i, m in enumerate(history) if m.get("id") == covers and m.get("id") is not None), None)
    if start is None:
        start = history.index(compaction)
    return [m for m in history[start + 1:] if _in_context(m)]


def trim_result_text(text: str, limit: Optional[int] = None) -> str:
    """Bound one tool result for storage, keeping the start which is usually the useful part."""
    limit = limit or settings.CONTEXT_TOOL_RESULT_CHARS
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[trimmed {len(text) - limit} characters]"


def cap_message_results(records: List[Dict[str, Any]], limit: Optional[int] = None) -> None:
    """Keep one message's stored results under a total size, dropping the oldest first."""
    limit = limit or settings.CONTEXT_TOOL_RESULTS_MESSAGE_CHARS
    total = sum(len(r.get("result") or "") for r in records)
    for record in records:
        if total <= limit:
            break
        text = record.get("result")
        if not text:
            continue
        total -= len(text)
        record["result"] = None
        record["result_dropped"] = True


def _stub_for(record: Dict[str, Any], note: str) -> str:
    stub: Dict[str, Any] = {"status": record.get("status") or "unknown", "note": note}
    if record.get("summary"):
        stub["summary"] = record["summary"]
    if record.get("error"):
        stub["error"] = record["error"]
    return json.dumps(stub, ensure_ascii=False)


def _tool_message(record: Dict[str, Any]) -> Dict[str, Any]:
    if record.get("status") == "completed":
        if record.get("result"):
            content = record["result"]
        else:
            content = _stub_for(record, "result not retained")
    else:
        content = _stub_for(record, "the call did not complete")
    return {"role": "tool", "tool_call_id": str(record["id"]), "content": content}


# How much of a malformed argument string is kept for the record and the replay.
MALFORMED_ARGUMENTS_KEEP = 1500


def safe_arguments(raw: Any) -> str:
    """Argument text that any OpenAI-compatible server accepts in a replayed tool call.

    A model's own call is replayed verbatim so the cache prefix holds. When
    the text is not a JSON object (an output cut mid-call, say), replaying
    it makes strict servers reject the whole request, so the replay carries
    a JSON object that quotes the broken text instead.
    """
    if raw is None or raw == "":
        return "{}"
    if not isinstance(raw, str):
        try:
            return json.dumps(raw, ensure_ascii=False)
        except (TypeError, ValueError):
            return "{}"
    try:
        if isinstance(json.loads(raw), dict):
            return raw
    except json.JSONDecodeError:
        pass
    kept = raw[:MALFORMED_ARGUMENTS_KEEP]
    note = "not valid JSON" + (f"; {len(raw) - len(kept)} more characters omitted" if len(raw) > len(kept) else "")
    return json.dumps({"malformed_arguments": kept, "note": note}, ensure_ascii=False)


def _call_entries(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    entries = []
    for record in records:
        arguments = record.get("raw_arguments")
        if not isinstance(arguments, str):
            arguments = record.get("arguments") or {}
        # A former tool name is replayed as today's, so old turns teach the current names.
        entries.append({"id": str(record["id"]), "name": current_name(record.get("name") or "tool"), "arguments": safe_arguments(arguments)})
    return entries


def summary_messages(compaction: Dict[str, Any]) -> List[Dict[str, Any]]:
    """How a stored compaction is shown to the model: a user turn carrying the
    summary and a short acknowledgement, so the kept turns follow naturally."""
    content = f"{SUMMARY_LEAD}\n\n{compaction.get('text', '')}"
    memory = (compaction.get("raw_payload") or {}).get("memory")
    if memory:
        content = f"{content}\n\n{MEMORY_LEAD}\n\n{memory}"
    todo = (compaction.get("raw_payload") or {}).get("todo")
    if todo:
        from app.services.bot_todo import render_todo

        content = f"{content}\n\n{TODO_LEAD}\n\n{render_todo(todo)}"
    return [
        {"role": "user", "content": content, "image_url": None},
        {"role": "assistant", "content": "Understood. I will continue from that summary.", "image_url": None},
    ]


def memory_note_messages(note: Dict[str, Any]) -> List[Dict[str, Any]]:
    """How a user edit of the notes is shown to the model, in place."""
    text = note.get("text") or ""
    if (note.get("raw_payload") or {}).get("by") == "rewind":
        body = f"{REWIND_NOTES_LEAD}\n\n{text}" if text else REWIND_NOTES_LEAD + "\n\n(empty)"
    else:
        body = f"{MEMORY_EDIT_LEAD}\n\n{text}" if text else "The user cleared your notes (memory)."
    return [
        {"role": "user", "content": body, "image_url": None},
        {"role": "assistant", "content": "Noted.", "image_url": None},
    ]


def todo_note_messages(note: Dict[str, Any]) -> List[Dict[str, Any]]:
    """How a user edit of the to-do list is shown to the model, in place."""
    text = note.get("text") or "(empty)"
    lead = REWIND_TODO_LEAD if (note.get("raw_payload") or {}).get("by") == "rewind" else TODO_EDIT_LEAD
    return [
        {"role": "user", "content": f"{lead}\n\n{text}", "image_url": None},
        {"role": "assistant", "content": "Noted.", "image_url": None},
    ]


def build_history_messages(history: List[Dict[str, Any]], *, replay_tools: bool) -> List[Dict[str, Any]]:
    """Transcript -> provider-neutral messages.

    Starts at the latest compaction, if there is one, with its summary. With
    `replay_tools` (OpenAI-compatible providers), bot turns that used tools
    become assistant(tool_calls) + tool results + assistant(text). Without it
    (MUAPI), only the texts are used.
    """
    compaction = latest_compaction(history)
    messages: List[Dict[str, Any]] = summary_messages(compaction) if compaction else []
    for message in live_messages(history):
        sender = message.get("sender")
        if sender == "user":
            text = message.get("text", "") or ""
            scheduled = (message.get("raw_payload") or {}).get("scheduled")
            if isinstance(scheduled, dict):
                text = f"{SCHEDULED_LEAD.format(name=scheduled.get('name', ''), at=scheduled.get('at', ''))}\n\n{text}"
            delegated = (message.get("raw_payload") or {}).get("delegated")
            if isinstance(delegated, dict):
                text = f"{DELEGATED_LEAD.format(name=delegated.get('from_name') or 'the orchestrator')}\n\n{text}"
            demonstration = (message.get("raw_payload") or {}).get("demonstration")
            if isinstance(demonstration, dict) and demonstration.get("text"):
                from app.services.demonstrations import LEAD as DEMONSTRATION_LEAD

                text = f"{text}\n\n{DEMONSTRATION_LEAD}\n{demonstration['text']}"
            turn_context = ((message.get("raw_payload") or {}).get("turn_context") or {}).get("text")
            # Attached files follow the user's words: a header saying where
            # each one is, then its text (capped at upload time).
            blocks = [attachment_for_model(a) for a in (message.get("attachments") or []) if isinstance(a, dict)]
            if blocks:
                text = "\n\n".join(([text] if text.strip() else []) + blocks)
            if turn_context:
                text = f"{text}\n\n{TURN_CONTEXT_LEAD}\n\n{turn_context}"
            messages.append({"role": "user", "content": text, "image_url": message.get("image_url")})
            continue
        if is_memory_note(message):
            messages.extend(memory_note_messages(message))
            continue
        if is_todo_note(message):
            messages.extend(todo_note_messages(message))
            continue
        if sender != "bot":
            continue
        text = message.get("text", "") or ""
        records = [
            r for r in ((message.get("raw_payload") or {}).get("tool_calls") or [])
            if isinstance(r, dict) and r.get("id")
        ]
        if not replay_tools or not records:
            messages.append({"role": "assistant", "content": text, "image_url": None})
            continue
        messages.append({"role": "assistant", "content": "", "tool_calls": _call_entries(records)})
        messages.extend(_tool_message(record) for record in records)
        if text.strip():
            messages.append({"role": "assistant", "content": text, "image_url": None})
    return messages
