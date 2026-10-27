"""Conversation titles written by a model once the first exchange is done.

Until then a conversation is named after its first message. After the bot's
first reply, a short request asks for a title of a few words, in the
background: it waits for a model slot like any other request and never
delays a reply. It is a one-shot request, so the model server is asked not
to keep its prompt in the cache. A title the user gave is never replaced,
and a failed attempt leaves the first-message title in place.

The model is App Settings' title model when one is set (a small, fast one
does the job), else the bot's own.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from app.services import conversations
from app.services.identity import get_current_owner
from app.services.storage_service import StorageService, current_storage

logger = logging.getLogger(__name__)

TITLE_INSTRUCTION = (
    "Write a title of 3 to 6 words for the conversation below: what it is about, the way a person would name it in a list. "
    "No quotes, no closing punctuation, no \"Title:\". Reply with the title only."
)
EXCERPT_CHARS = 1500
MAX_TRIES = 2
# Who does not get a generated title: the user named it, it has one, or Cubicle named it for its purpose.
SETTLED = {"user", "generated", "fixed"}

_running: Set[Tuple[str, str]] = set()


def wants_title(record: Optional[Dict[str, Any]]) -> bool:
    if not record or record.get("purpose"):
        return False
    return record.get("title_source") not in SETTLED and int(record.get("title_tries") or 0) < MAX_TRIES


def first_exchange(messages: List[Dict[str, Any]]) -> Optional[Tuple[str, str]]:
    """The user's first message and the bot's first reply to it, trimmed; None until both exist."""
    for index, message in enumerate(messages):
        if message.get("sender") == "user" and str(message.get("text") or "").strip():
            reply = next((m for m in messages[index + 1:] if m.get("sender") == "bot" and str(m.get("text") or "").strip()), None)
            if reply is None:
                return None
            return str(message["text"]).strip()[:EXCERPT_CHARS], str(reply["text"]).strip()[:EXCERPT_CHARS]
    return None


def clean_generated(text: Any) -> str:
    """The title out of a model reply: its first line, without quotes, a "Title:" lead or closing punctuation."""
    line = next((part.strip() for part in str(text or "").splitlines() if part.strip()), "")
    line = re.sub(r"^\W*title\s*:\s*", "", line, flags=re.IGNORECASE)
    line = line.strip(" \t\"'`*#“”‘’").rstrip(".!?;:,").strip(" \t\"'`*#“”‘’")
    return conversations.clean_title(line)


def title_model(bot: Dict[str, Any], config: Any, storage: StorageService) -> str:
    chosen = str(storage.get_settings().get("title_model") or "").strip()
    return chosen or str(bot.get("model") or "").strip() or config.default_model


async def generate(thread_id: str, storage: Optional[StorageService] = None) -> Optional[str]:
    """Write the conversation's title if it still wants one. Returns the title, or None."""
    from app.services import llm_service

    storage = storage or current_storage()
    record = conversations.get(thread_id, storage)
    if not wants_title(record):
        return None
    exchange = first_exchange(storage.get_messages(thread_id=thread_id))
    if exchange is None:
        return None
    bot = next((b for b in storage.get_bots() if b.get("id") == record["bot_id"]), None)
    if bot is None:
        return None
    config = llm_service.current_llm_config()
    model = title_model(bot, config, storage)
    prompt = f"{TITLE_INSTRUCTION}\n\nThe user wrote:\n{exchange[0]}\n\nThe assistant replied:\n{exchange[1]}"
    pieces: List[str] = []
    ok = True
    try:
        async for event in llm_service.stream_chat_completion(
            model=model, messages=[{"role": "user", "content": prompt}], system_prompt="", config=config, tools=None, one_shot=True
        ):
            if event.get("type") == "content.delta":
                pieces.append(event.get("delta") or "")
            elif event.get("type") == "turn.completed" and not event.get("ok", True):
                ok = False
    except Exception as exc:
        logger.warning("Titling conversation %s failed: %s", thread_id, exc)
        ok = False
    title = clean_generated("".join(pieces)) if ok else ""
    # The user may have renamed it while the model was writing.
    fresh = conversations.get(thread_id, storage)
    if not wants_title(fresh):
        return None
    tries = int(fresh.get("title_tries") or 0) + 1
    if not title:
        conversations.update(thread_id, {"title_tries": tries}, storage)
        return None
    conversations.update(thread_id, {"title": title, "title_source": "generated", "title_tries": tries}, storage)
    return title


def schedule(thread_id: str) -> None:
    """Title the conversation in the background, once at a time; nothing when there is no event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    key = (get_current_owner(), thread_id)
    if key in _running:
        return
    _running.add(key)

    async def run() -> None:
        try:
            await generate(thread_id)
        except Exception as exc:  # a title is a nicety
            logger.warning("Titling conversation %s failed: %s", thread_id, exc)
        finally:
            _running.discard(key)

    loop.create_task(run())
