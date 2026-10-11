"""Compaction: fold the older part of a thread into one stored summary.

The context builder never rewrites a message the model has already seen, so
the provider's prompt cache resumes from the end of the last turn and a turn
costs only what it adds. The one thing that still has to give is the window:
a long thread fills it. Compaction is the planned answer. It asks the model
to summarise everything older than the last few turns, stores that summary
as a `compaction` message in the thread, and from then on the context starts
there. The raw messages stay in the database for display and audit.

The summary request repeats the conversation's own prefix (same system
prompt, same history), so the prefill is served from the cache and the call
costs roughly the instruction plus the summary.
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.services import llm_service
from app.services.context_builder import COMPACTION_ITEM_TYPE, build_history_messages, is_compaction, live_messages
from app.services.storage_service import StorageService, storage_service

logger = logging.getLogger(__name__)

SUMMARY_INSTRUCTION = """The conversation above is about to be compacted: everything before this message will be replaced by your summary, and only the most recent turns will remain. Write the summary that lets you continue the work without that history. It must be concrete and complete on these points, in this order, under headings:

1. Task and goal: what the user is trying to achieve, and the current state of it.
2. Decisions and constraints: choices made, preferences the user stated, things ruled out, and why.
3. Work done: files, resources or systems touched, with paths, names and identifiers exactly as they appear; commands or tools that mattered and what they returned.
4. Open items: what is unfinished, what the next step was going to be, and anything that was failing or unverified.
5. Facts to remember: identifiers, values, URLs, error messages and other specifics that would be costly to rediscover.

Write it as notes to yourself, in plain prose and lists, without preamble. Keep it under 800 words. Do not call tools."""



def split_for_compaction(history: List[Dict[str, Any]], keep_recent_turns: Optional[int] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split the live part of a thread into (older, recent).

    `older` is what gets summarised: the messages after the last compaction
    and before the last `keep_recent_turns` user messages. `recent` begins at
    the first kept user message. Either may be empty.
    """
    keep = settings.CONTEXT_KEEP_RECENT_TURNS if keep_recent_turns is None else keep_recent_turns
    keep = max(1, keep)
    live = live_messages(history)
    user_indexes = [i for i, m in enumerate(live) if m.get("sender") == "user"]
    if len(user_indexes) <= keep:
        return [], live
    cut = user_indexes[-keep]
    return live[:cut], live[cut:]


def last_prompt_tokens(history: List[Dict[str, Any]]) -> Optional[int]:
    """The prompt size the provider reported on the latest bot reply, if any."""
    for message in reversed(history):
        if message.get("sender") != "bot":
            continue
        usage = (message.get("raw_payload") or {}).get("usage") or {}
        tokens = usage.get("prompt_tokens")
        return int(tokens) if isinstance(tokens, (int, float)) and tokens > 0 else None
    return None


def should_compact(history: List[Dict[str, Any]], context_window: Optional[int]) -> bool:
    """True when the last reported prompt crossed the configured share of the window."""
    share = settings.CONTEXT_COMPACT_AT
    if not context_window or share <= 0:
        return False
    tokens = last_prompt_tokens(history)
    return tokens is not None and tokens >= share * context_window


async def compact_thread(
    thread_id: str,
    *,
    reason: str,
    model: str,
    system_prompt: str,
    config: Any = None,
    storage: StorageService = storage_service,
    keep_recent_turns: Optional[int] = None,
    replay_tools: bool = True,
) -> Optional[Dict[str, Any]]:
    """Summarise the older part of a thread and store the summary. Returns the stored message, or None when there was nothing to compact."""
    history = storage.get_messages(thread_id=thread_id)
    older, recent = split_for_compaction(history, keep_recent_turns)
    if not older:
        return None

    # The summariser sees exactly what a turn would have seen up to the cut:
    # the previous summary (if any) and the older turns with their tool
    # results, so the provider's cache serves the prefix.
    prior = [m for m in history if is_compaction(m)]
    messages = build_history_messages(prior[-1:] + older, replay_tools=replay_tools)
    messages.append({"role": "user", "content": SUMMARY_INSTRUCTION})

    pieces: List[str] = []
    usage: Dict[str, Any] = {}
    async for event in llm_service.stream_chat_completion(model=model, messages=messages, system_prompt=system_prompt, config=config, tools=None):
        if event["type"] == "content.delta":
            pieces.append(event["delta"])
        elif event["type"] == "usage":
            usage = {"prompt_tokens": event.get("prompt_tokens"), "completion_tokens": event.get("completion_tokens")}
        elif event["type"] == "turn.completed" and not event.get("ok", True):
            raise RuntimeError(event.get("error") or "the summary request failed")
    summary = "".join(pieces).strip()
    if not summary:
        raise RuntimeError("the model returned an empty summary")

    message = {
        "id": f"msg-{uuid.uuid4().hex[:6]}",
        "thread_id": thread_id,
        "bot_id": thread_id,
        "sender": "system",
        "text": summary,
        "created_at": datetime.now().isoformat(),
        "model": model,
        "item_type": COMPACTION_ITEM_TYPE,
        "raw_payload": {
            "reason": reason,
            "messages_compacted": len(older),
            "covers_through": older[-1].get("id"),
            "prompt_tokens_before": last_prompt_tokens(history),
            "summary_usage": usage or None,
        },
    }
    storage.add_message(message)
    logger.info("Compacted thread %s: %d messages summarised (%s)", thread_id, len(older), reason)
    return message
