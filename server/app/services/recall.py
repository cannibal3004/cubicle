"""Recall: what a bot said and heard earlier, found again by similarity.

The context a turn sees is the latest compaction summary plus the messages
after it; everything older is gone from the prompt. Each stored message
(the user's, the bot's reply, and each compaction summary) is also embedded
under kind "message", and when a conversation has been compacted, the turn
retrieves the few past exchanges that match the current message and adds
them to the prompt as "earlier in this conversation". The notes stay the
short standing facts; this is the long tail.

Indexing runs in the background and never holds up a message. A bot whose
history predates this feature is backfilled once, the first time a turn
needs it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set

from app.services import retrieval
from app.services.context_builder import is_compaction, latest_compaction, live_messages
from app.services.identity import get_current_owner, owner_context

logger = logging.getLogger(__name__)

KIND = "message"
# How much of one message is embedded: a long reply is cut into chunks by the
# chunker; this caps what one message may contribute.
MAX_MESSAGE_CHARS = 20_000
RECALL_LIMIT = 4
_backfilled: Set[str] = set()


def message_ref(bot_id: str, message_id: str) -> str:
    return f"{bot_id}:{message_id}"


def indexable(message: Dict[str, Any]) -> bool:
    text = str(message.get("text") or "").strip()
    if not text or not message.get("id"):
        return False
    sender = message.get("sender")
    return sender in ("user", "bot") or is_compaction(message)


def _meta(message: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "sender": "summary" if is_compaction(message) else str(message.get("sender") or ""),
        "created_at": str(message.get("created_at") or ""),
        "message_id": str(message.get("id") or ""),
        "name": _label(message),
    }


def _label(message: Dict[str, Any]) -> str:
    when = str(message.get("created_at") or "")[:16].replace("T", " ")
    if is_compaction(message):
        who = "summary of earlier conversation"
    elif message.get("sender") == "user":
        who = "the user"
    else:
        who = "you"
    return f"{who}, {when}" if when else who


async def index_message(bot_id: str, message: Dict[str, Any], *, config: Optional[retrieval.RetrievalConfig] = None, storage=None, embed=None) -> int:
    if not indexable(message):
        return 0
    text = str(message.get("text") or "")[:MAX_MESSAGE_CHARS]
    return await retrieval.index_text(KIND, message_ref(bot_id, str(message["id"])), text, meta=_meta(message), config=config, storage=storage, embed=embed)


def schedule_index(bot_id: str, message: Dict[str, Any]) -> None:
    """Embed one stored message in the background, under the caller's account."""
    if not indexable(message):
        return
    try:
        config = retrieval.current_config()
    except Exception:
        return
    if not config.enabled:
        return
    owner = get_current_owner()

    async def run() -> None:
        with owner_context(owner):
            try:
                await index_message(bot_id, message, config=config)
            except Exception as exc:  # recall is a nicety; the message itself is stored
                logger.warning("Could not index message %s for recall: %s", message.get("id"), exc)

    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        pass


async def backfill(bot_id: str, messages: Iterable[Dict[str, Any]], *, config: Optional[retrieval.RetrievalConfig] = None, storage=None, embed=None) -> int:
    """Embed every indexable message of a bot that has none indexed yet."""
    done = 0
    for message in messages:
        try:
            done += 1 if await index_message(bot_id, message, config=config, storage=storage, embed=embed) else 0
        except retrieval.RetrievalError as exc:
            logger.warning("Backfill of %s stopped: %s", bot_id, exc)
            break
    return done


def ensure_backfilled(bot_id: str, history: List[Dict[str, Any]], *, config: Optional[retrieval.RetrievalConfig] = None, storage=None) -> bool:
    """Schedule a one-time backfill when the bot has history but no vectors. True when scheduled."""
    if bot_id in _backfilled:
        return False
    config = config or retrieval.current_config()
    if not config.enabled:
        return False
    candidates = [m for m in history if indexable(m)]
    if not candidates:
        return False
    if retrieval.index_for(storage).count(kind=KIND, ref_prefix=f"{bot_id}:") > 0:
        _backfilled.add(bot_id)
        return False
    _backfilled.add(bot_id)
    owner = get_current_owner()

    async def run() -> None:
        with owner_context(owner):
            try:
                count = await backfill(bot_id, candidates, config=config, storage=storage)
                logger.info("Recall backfill for %s: %d messages indexed", bot_id, count)
            except Exception as exc:
                logger.warning("Recall backfill for %s failed: %s", bot_id, exc)

    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        return False
    return True


async def recall_for_turn(bot_id: str, query: str, history: List[Dict[str, Any]], *, config: Optional[retrieval.RetrievalConfig] = None, storage=None, embed=None, limit: int = RECALL_LIMIT) -> str:
    """Past exchanges that match `query` and are no longer in the context, as a prompt block."""
    config = config or retrieval.current_config()
    if not config.enabled or not (query or "").strip():
        return ""
    if latest_compaction(history) is None:
        return ""  # nothing has left the context yet
    live_ids = {str(m.get("id")) for m in live_messages(history)}
    compaction = latest_compaction(history)
    if compaction:
        live_ids.add(str(compaction.get("id")))
    try:
        hits = await retrieval.retrieve(query, kind=KIND, ref_prefix=f"{bot_id}:", limit=limit * 3, config=config, storage=storage, embed=embed, use_reranker=False)
    except Exception as exc:
        logger.warning("Recall failed for %s: %s", bot_id, exc)
        return ""
    hits = [hit for hit in hits if hit.meta.get("message_id") not in live_ids and hit.text.strip() != query.strip()]
    if not hits:
        return ""
    if len(hits) > limit:
        ranked = await retrieval.rerank(query, [hit.text for hit in hits], limit, config)
        if ranked:
            picked = []
            for position, score in ranked[:limit]:
                if 0 <= position < len(hits):
                    hits[position].score = score
                    picked.append(hits[position])
            hits = picked
        else:
            hits = hits[:limit]
    hits.sort(key=lambda hit: hit.meta.get("created_at") or "")
    return retrieval.render_hits(
        hits,
        heading="Earlier in this conversation, recalled by similarity (these are no longer in the context above; the summary may not mention them):",
    )
