"""Compaction: fold the older part of a thread into one stored summary.

The context builder never rewrites a message the model has already seen, so
the provider's prompt cache resumes from the end of the last turn and a turn
costs only what it adds. The one thing that still has to give is the window:
a long thread fills it. Compaction is the planned answer. It asks the model
to summarise everything older than a budget of recent tokens, stores that
summary as a `compaction` message in the thread, and from then on the context
starts there. The raw messages stay in the database for display and audit.

What is kept is measured in tokens, not turns: a turn of this harness can be
a one-line answer or ninety thousand tokens of tool calls, so a turn count
says nothing about how much of the window the kept part will take.

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
from app.services.storage_service import StorageService, current_storage

logger = logging.getLogger(__name__)

SUMMARY_INSTRUCTION = """The conversation above is about to be compacted: everything before this message will be replaced by your summary, and only the most recent turns will remain. Write the summary that lets you continue the work without that history. It must be concrete and complete on these points, in this order, under headings:

1. Task and goal: what the user is trying to achieve, and the current state of it.
2. Decisions and constraints: choices made, preferences the user stated, things ruled out, and why.
3. Work done: files, resources or systems touched, with paths, names and identifiers exactly as they appear; commands or tools that mattered and what they returned.
4. Open items: what is unfinished, what the next step was going to be, and anything that was failing or unverified.
5. Facts to remember: identifiers, values, URLs, error messages and other specifics that would be costly to rediscover.

Write it as notes to yourself, in plain prose and lists, without preamble. Keep it under 800 words. Do not call tools."""

# Used when the provider's own count is not available for a stretch of the
# thread. Code and JSON run closer to three characters a token, so this
# slightly understates them; the budget is a target, not a hard limit.
CHARS_PER_TOKEN = 4


def estimate_tokens(messages: List[Dict[str, Any]]) -> int:
    """Rough size of provider-neutral messages: text plus tool call arguments."""
    chars = 0
    for message in messages:
        chars += len(message.get("content") or "")
        for call in message.get("tool_calls") or []:
            chars += len(call.get("name") or "") + len(call.get("arguments") or "")
    return -(-chars // CHARS_PER_TOKEN)


def _prompt_tokens_of(message: Dict[str, Any]) -> Optional[int]:
    usage = (message.get("raw_payload") or {}).get("usage") or {}
    tokens = usage.get("prompt_tokens")
    return int(tokens) if isinstance(tokens, (int, float)) and tokens > 0 else None


def turn_slices(live: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Group live messages into turns: each user message starts one."""
    turns: List[List[Dict[str, Any]]] = []
    for message in live:
        if message.get("sender") == "user" or not turns:
            turns.append([message])
        else:
            turns[-1].append(message)
    return turns


def turn_sizes(turns: List[List[Dict[str, Any]]], *, replay_tools: bool = True) -> List[int]:
    """Tokens each turn adds to the context.

    Where this turn's reply and the next turn's reply both carry the
    provider's prompt count, the difference between them is exact: it is what
    the context grew by between the two requests (this reply, with its tool
    calls and results, plus the next user message). Otherwise, and always for
    the newest turn, the size is estimated from the characters the builder
    would send.
    """
    prompts = [next((_prompt_tokens_of(m) for m in reversed(turn) if m.get("sender") == "bot"), None) for turn in turns]
    sizes: List[int] = []
    for i, turn in enumerate(turns):
        here, following = prompts[i], prompts[i + 1] if i + 1 < len(turns) else None
        if here and following and following > here:
            sizes.append(following - here)
        else:
            sizes.append(estimate_tokens(build_history_messages(turn, replay_tools=replay_tools)))
    return sizes


def split_for_compaction(
    history: List[Dict[str, Any]], keep_recent_tokens: Optional[int] = None, *, replay_tools: bool = True
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split the live part of a thread into (older, recent).

    `recent` is the newest whole turns that fit in the token budget, and
    always at least the newest turn, however large. `older` is what gets
    summarised: the messages after the last compaction and before `recent`.
    Either may be empty.
    """
    budget = settings.CONTEXT_KEEP_RECENT_TOKENS if keep_recent_tokens is None else keep_recent_tokens
    live = live_messages(history)
    turns = turn_slices(live)
    if len(turns) <= 1:
        return [], live
    sizes = turn_sizes(turns, replay_tools=replay_tools)
    kept, total = 1, sizes[-1]
    while kept < len(turns) and total + sizes[-kept - 1] <= budget:
        total += sizes[-kept - 1]
        kept += 1
    cut = len(turns) - kept
    older = [m for turn in turns[:cut] for m in turn]
    recent = [m for turn in turns[cut:] for m in turn]
    return older, recent


def last_prompt_tokens(history: List[Dict[str, Any]]) -> Optional[int]:
    """The prompt size the provider reported on the latest bot reply, if any."""
    for message in reversed(history):
        if message.get("sender") == "bot":
            return _prompt_tokens_of(message)
    return None


def over_budget(prompt_tokens: Optional[int], context_window: Optional[int]) -> bool:
    """True when a reported prompt size crossed the configured share of the window."""
    return _past(prompt_tokens, context_window, settings.CONTEXT_COMPACT_AT)


def at_hard_limit(prompt_tokens: Optional[int], context_window: Optional[int]) -> bool:
    """True when a reported prompt size leaves no safe room for another tool round."""
    return _past(prompt_tokens, context_window, settings.CONTEXT_STOP_AT)


def _past(prompt_tokens: Optional[int], context_window: Optional[int], share: float) -> bool:
    if not context_window or share <= 0 or not prompt_tokens:
        return False
    return prompt_tokens >= share * context_window


def should_compact(history: List[Dict[str, Any]], context_window: Optional[int]) -> bool:
    """True when the last reported prompt crossed the configured share of the window."""
    return over_budget(last_prompt_tokens(history), context_window)


async def compact_thread(
    thread_id: str,
    *,
    reason: str,
    model: str,
    system_prompt: str,
    config: Any = None,
    storage: Optional[StorageService] = None,
    keep_recent_tokens: Optional[int] = None,
    replay_tools: bool = True,
) -> Optional[Dict[str, Any]]:
    """Summarise the older part of a thread and store the summary. Returns the stored message, or None when there was nothing to compact."""
    storage = storage or current_storage()
    history = storage.get_messages(thread_id=thread_id)
    older, recent = split_for_compaction(history, keep_recent_tokens, replay_tools=replay_tools)
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

    # The bot's notes as they stand now travel with the summary, so after the
    # cut the model still sees them (their earlier edits are gone with the
    # older turns), and sees the same text every turn until the next cut.
    bot = next((b for b in storage.get_bots() if b.get("id") == thread_id), {})
    memory = str(bot.get("memory") or "")
    todo = list(bot.get("todo") or [])

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
            "memory": memory,
            "todo": todo,
        },
    }
    storage.add_message(message)
    # The summary is itself worth recalling later, once another compaction folds it away.
    from app.services import recall

    recall.schedule_index(thread_id, message)
    logger.info("Compacted thread %s: %d messages summarised (%s)", thread_id, len(older), reason)
    return message
