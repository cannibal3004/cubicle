"""How hard the model thinks on a turn: the reasoning effort it is sent.

A turn sends one effort level (`reasoning_effort`) for all its rounds. It
comes from, first to last: the conversation's own setting (the composer's
chip or /think), the bot's, then the model server setting in App Settings.

"auto" asks the decision model that auto-approval uses (decider-4b) a few
yes/no questions about the user's message, with the exchange before it for
context ("do it" means whatever was just proposed), and picks a light or a
deep level. It leans deep: light only when the answers are clearly no.

Auto is sticky on purpose. halogen-flash-server keeps a separate prompt
cache for each effort level (measured 2026-10-28): moving a conversation to
a level it has not used re-reads the whole prompt, and moving back re-reads
what came since. So Auto stays where the conversation is unless the decider
is clear, and compaction sends the conversation's last level so its prefix
still comes from the cache.

Each turn's pick is logged with how the turn went (`thinking_picks`): the
record for tuning the questions and thresholds before Auto becomes a default.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.services.action_gateway import redact_sensitive
from app.services.identity import get_current_owner
from app.services.storage_service import current_storage

logger = logging.getLogger(__name__)

AUTO = "auto"
LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_ALIASES = {"off": "none", "default": "", "inherit": "", "bot": ""}

# The questions, answered yes/no on the user's latest message. Each states its
# direction ("as opposed to ..."): the decider reads described options far
# better than rules (see approval_gate.QUESTIONS).
QUESTIONS: Dict[str, str] = {
    "complex": (
        "Does answering the user's message well need careful multi-step reasoning, such as planning, debugging, analysis, "
        "math, or writing or changing code, as opposed to a quick lookup, running one simple command, or a short reply?"
    ),
    "stakes": (
        "Could a careless answer to the user's message cause real harm, lose data, or waste a lot of the user's work, "
        "as opposed to being easy to check and redo?"
    ),
}
# The highest answer decides. A first pick is light below FIRST; after a light
# turn it takes UP or more to go deep, after a deep turn a clear no (below
# DOWN) to go light. Going light is made hard on purpose: on a trivial reply
# a lower level saves little thinking, while a level the conversation has not
# used yet re-reads all of it. Measured with decider-4b on 2026-10-28: quick
# asks ("thanks", "what time is it in Tokyo", "curl ifconfig.me") scored
# 0.03-0.32, real work (debugging, planning, code, "do it" after a plan,
# deleting snapshots) 0.56-0.85.
FIRST = 0.3
UP = 0.4
DOWN = 0.1
MESSAGE_CHARS = 2000
CONTEXT_CHARS = 1000


class ThinkingError(ValueError):
    """A thinking setting that is not one."""


def clean_setting(value: Any) -> str:
    """"" (follow the next level up), "auto", or an effort level."""
    text = str(value or "").strip().lower()
    text = _ALIASES.get(text, text)
    if text in ("", AUTO) or text in LEVELS:
        return text
    raise ThinkingError("Thinking is auto, default, or one of: " + ", ".join(LEVELS) + ".")


def light_level() -> str:
    return settings.THINKING_AUTO_LIGHT or "low"


def deep_level() -> str:
    return settings.THINKING_AUTO_DEEP or "xhigh"


def offered_levels() -> List[str]:
    """What the menus and /think offer (THINKING_LEVELS); any level in LEVELS is accepted all the same."""
    wanted = {part.strip() for part in settings.THINKING_LEVELS.split(",")}
    return [level for level in LEVELS if level in wanted] or ["none", "low", "medium", "high", "xhigh"]


def configured(bot: Optional[Dict[str, Any]], conversation: Optional[Dict[str, Any]], app_effort: str) -> Tuple[str, str]:
    """(setting, where it comes from: conversation | bot | app)."""
    for source, record in (("conversation", conversation), ("bot", bot)):
        try:
            setting = clean_setting((record or {}).get("thinking"))
        except ThinkingError:
            setting = ""
        if setting:
            return setting, source
    return app_effort or "", "app"


def standing_level(bot: Optional[Dict[str, Any]], conversation: Optional[Dict[str, Any]], app_effort: str) -> str:
    """The level to send when nothing is decided per message (a compaction, a fixed setting):
    for Auto, where the conversation last was, so the prompt cache still fits."""
    setting, _ = configured(bot, conversation, app_effort)
    if setting != AUTO:
        return setting
    return str((conversation or {}).get("thinking_last") or deep_level())


@dataclass
class Pick:
    level: str                       # sent as reasoning_effort; "" sends nothing
    setting: str                     # what was configured ("auto" or a level)
    source: str                      # conversation | bot | app
    auto: bool = False
    previous: str = ""
    scores: Dict[str, float] = field(default_factory=dict)
    reason: str = ""
    latency_ms: Optional[int] = None

    def to_event(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"level": self.level, "auto": self.auto, "source": self.source}
        if self.auto:
            out.update({"scores": {k: round(v, 3) for k, v in self.scores.items()}, "reason": self.reason})
        return out


def decide(scores: Dict[str, float], previous: str) -> Tuple[str, str]:
    """(level, why) from the decider's answers and where the conversation was."""
    light, deep = light_level(), deep_level()
    top = max(scores, key=scores.get)
    value = scores[top]
    if previous == deep:
        return (light, f"{top} {value:.2f} below {DOWN:g}") if value < DOWN else (deep, f"{top} {value:.2f}; staying {deep}")
    if previous == light:
        return (deep, f"{top} {value:.2f}, at least {UP:g}") if value >= UP else (light, f"{top} {value:.2f}; staying {light}")
    return (light, f"{top} {value:.2f} below {FIRST:g}") if value < FIRST else (deep, f"{top} {value:.2f}")


def _clip(text: Any, limit: int) -> str:
    text = str(redact_sensitive(str(text or "")))
    return text if len(text) <= limit else text[: limit - 3] + "..."


def build_state(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The latest user message and the exchange before it; never tool output."""
    said = [m for m in history if m.get("sender") in ("user", "bot") and str(m.get("text") or "").strip()]
    latest = next((i for i in range(len(said) - 1, -1, -1) if said[i].get("sender") == "user"), None)
    if latest is None:
        return {}
    state: Dict[str, Any] = {"message": _clip(said[latest].get("text"), MESSAGE_CHARS)}
    before = said[:latest]
    reply = next((m for m in reversed(before) if m.get("sender") == "bot"), None)
    if reply is not None:
        state["previous_reply"] = _clip(reply.get("text"), CONTEXT_CHARS)
        asked = next((m for m in reversed(before[: before.index(reply)]) if m.get("sender") == "user"), None)
        if asked is not None:
            state["previous_message"] = _clip(asked.get("text"), CONTEXT_CHARS // 2)
    return state


async def choose(bot: Optional[Dict[str, Any]], conversation: Optional[Dict[str, Any]], history: List[Dict[str, Any]], app_effort: str) -> Pick:
    """This turn's level. Never raises: without an answer, Auto stays where it was (or goes deep)."""
    setting, source = configured(bot, conversation, app_effort)
    if setting != AUTO:
        return Pick(level=setting, setting=setting, source=source)
    previous = str((conversation or {}).get("thinking_last") or "")
    if previous not in (light_level(), deep_level()):
        previous = ""
    fallback = previous or deep_level()
    pick = Pick(level=fallback, setting=AUTO, source=source, auto=True, previous=previous)
    state = build_state(history)
    if not state:
        pick.reason = "no message to judge"
        return pick
    from app.services.approval_gate import DeciderError, approval_gate

    url = approval_gate._config()["url"]
    if not url:
        pick.reason = "no decision server configured"
        return pick
    started = time.monotonic()
    try:
        pick.scores = await approval_gate.client_factory(url, settings.DECIDER_TIMEOUT_SECONDS).score(state, QUESTIONS)
    except DeciderError as exc:
        pick.reason = f"decision server: {exc}"
        logger.warning("Thinking could not be chosen: %s", exc)
        return pick
    finally:
        pick.latency_ms = int((time.monotonic() - started) * 1000)
    pick.level, pick.reason = decide(pick.scores, previous)
    return pick


# ---- the record ---------------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record(pick: Pick, *, thread_id: str, message_id: str, bot_id: str, model: str) -> str:
    """Log a turn's pick; returns its id for `finish`."""
    pick_id = f"tp-{uuid.uuid4().hex[:10]}"
    payload = {**asdict(pick), "bot_id": bot_id, "model": model}
    try:
        with current_storage().database.connect() as connection:
            connection.execute(
                "INSERT INTO thinking_picks(id, owner_id, thread_id, message_id, created_at, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (pick_id, get_current_owner(), thread_id, message_id, _now(), json.dumps(payload)),
            )
    except Exception as exc:  # the log is a nicety; the turn goes on
        logger.warning("Could not log the thinking pick: %s", exc)
    return pick_id


def _amend(where: str, args: Tuple[Any, ...], changes: Dict[str, Any]) -> int:
    try:
        with current_storage().database.connect() as connection:
            rows = connection.execute(f"SELECT id, payload FROM thinking_picks WHERE owner_id = ? AND {where}", (get_current_owner(), *args)).fetchall()
            for pick_id, payload in rows:
                connection.execute("UPDATE thinking_picks SET payload = ? WHERE id = ?", (json.dumps({**json.loads(payload), **changes}), pick_id))
        return len(rows)
    except Exception as exc:
        logger.warning("Could not update the thinking log: %s", exc)
        return 0


def finish(pick_id: str, outcome: Dict[str, Any]) -> None:
    """How the turn went: duration, rounds, why it stopped early, loops cut."""
    _amend("id = ?", (pick_id,), {"outcome": outcome})


def mark_undone(message_ids: List[str]) -> int:
    """The user took these replies back (undo or edit): a sign the turn missed."""
    count = 0
    for message_id in message_ids:
        count += _amend("message_id = ?", (message_id,), {"undone": True})
    return count


def recent(limit: int = 200) -> List[Dict[str, Any]]:
    with current_storage().database.connect() as connection:
        rows = connection.execute(
            "SELECT id, thread_id, message_id, created_at, payload FROM thinking_picks WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?",
            (get_current_owner(), max(1, min(int(limit), 1000))),
        ).fetchall()
    return [{"id": r[0], "thread_id": r[1], "message_id": r[2], "created_at": r[3], **json.loads(r[4])} for r in rows]
