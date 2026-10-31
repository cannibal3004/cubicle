"""Automatic memory: the lasting facts a bot picks up from its conversations, kept and recalled for it.

A bot's notes are the small, curated set it always sees; this is the long tail. When a bot has it on
(its own switch, else the account's default), each finished reply is read in the background by the
memory model (App Settings, else the default model; always with thinking off): the exchange, the
bot's notes (so nothing in them is kept twice) and the bot's memories nearest to it. The model says
what to add, which existing memory a new fact replaces, and which turned out wrong. A replaced memory
is not deleted: it stays in the history, linked to its successor, so what changed and when can be
seen. Known secrets are removed before the model reads anything, and a memory that still looks like
a credential is dropped.

Before each turn the bot's current memories that match the message are added to what it is shown
(services/retrieval when enabled; else a word match), and memory_search lets it look itself. The user
reads, edits and deletes them in the bot's settings.

Each memory: id, subject (what it is about: a host, a person, a service), text, created_at, source
(conversation and message), state (current | replaced | forgotten), replaced_by / replaces, and
edited when the user changed it. Stored per bot in bot_memories.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from app.services.action_gateway import ActionDefinition, ActionGateway, ActionInvocation, action_gateway
from app.services.identity import get_current_owner, owner_context
from app.services.storage_service import StorageService, current_storage

logger = logging.getLogger(__name__)

KIND = "memory"
SUBJECT_MAX = 80
TEXT_MAX = 600
MAX_CURRENT = 3000  # per bot; the oldest current memories make way beyond it
RELATED_FOR_EXTRACTION = 12
RECALL_LIMIT = 6
EXCHANGE_CHARS = 4000
NOTES_CHARS = 6000
SEARCH_ACTION = "memory.search"

# The settings: an account's default for bots that have not chosen, and the model (app-wide).
DEFAULT_SETTING = "auto_memory_default"
MODEL_SETTING = "memory_model"

# What a credential looks like when the model kept one anyway: a password, key, token or secret given a value.
_CREDENTIAL = re.compile(
    # The value: no spaces, with a digit or a symbol in it (so "the key is stored in the vault" is not one).
    r"(pass(word|wd|phrase)?|pwd|api[\s_-]?key|access[\s_-]?key|secret|token|bearer)\b[^.\n]{0,24}?(:|=|\bis\b|\bwas\b)\s*['\"`]?"
    r"(?=[^\s'\"`]*[\d!@#$%^&*+=~])[^\s'\"`]{4,}",
    re.IGNORECASE,
)
_LOGIN_PAIR = re.compile(r"\b(login|credentials?|username)\b[^.\n]{0,40}?\S+\s*/\s*\S{4,}", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9][a-z0-9._-]*", re.IGNORECASE)

EXTRACTION_INSTRUCTIONS = """You keep an assistant's long-term memory: the lasting facts it learns in its conversations.

Read the exchange below and decide what is worth remembering for later conversations: facts about the
systems, places, people and arrangements the assistant works with (names, addresses, versions, owners,
schedules, decisions, what was set up or changed and how), and the user's lasting preferences and ways of
working. Leave out small talk, what is only true for the moment (a scan running now, "I'll check"), the
assistant's own steps that led nowhere, and anything already in the assistant's notes.

Never keep passwords, API keys, tokens, private keys or other credentials, even when they are given. If one
matters, keep only that it exists and where it is kept, never its value.

Each memory is one self-contained fact in a sentence, with the names it needs to stand on its own, and with
absolute dates (today is given) instead of "yesterday" or "next week". Give each a subject: the thing it is
about, in a few words (a host name, a person, a service, "the user's preferences").

Compare with the existing memories (numbered). When a new fact changes or contradicts one of them (a host
moved to a new address, a service was replaced, a decision changed), REPLACE that one: give its number and
the new fact, which says what is true now. When the exchange shows an existing memory is simply wrong or no
longer applies and nothing replaces it, FORGET it. Do not add what an existing memory already says.

Reply with JSON only, in this shape (an empty list when there is nothing to keep):
{"memories": [
  {"op": "add", "subject": "...", "text": "..."},
  {"op": "replace", "id": "3", "subject": "...", "text": "..."},
  {"op": "forget", "id": "5"}
]}"""


class MemoryError(RuntimeError):
    pass


# ---- storage --------------------------------------------------------------------------------


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _rows(bot_id: str, storage: Optional[StorageService] = None) -> List[Dict[str, Any]]:
    storage = storage or current_storage()
    with storage.database.connect() as connection:
        rows = connection.execute(
            "SELECT payload FROM bot_memories WHERE owner_id = ? AND bot_id = ? ORDER BY created_at, rowid", (storage.owner_id, bot_id)
        ).fetchall()
    out = []
    for (payload,) in rows:
        try:
            record = json.loads(payload)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _put(record: Dict[str, Any], storage: Optional[StorageService] = None) -> None:
    storage = storage or current_storage()
    with storage.database.connect() as connection:
        connection.execute(
            "INSERT INTO bot_memories(id, owner_id, bot_id, created_at, payload) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
            (record["id"], storage.owner_id, record["bot_id"], record["created_at"], json.dumps(record)),
        )


def list_memories(bot_id: str, *, include_history: bool = False, query: str = "", storage: Optional[StorageService] = None) -> List[Dict[str, Any]]:
    """The bot's memories, newest first: the current ones, or all with `include_history`; `query` matches words."""
    records = [r for r in _rows(bot_id, storage) if include_history or r.get("state") == "current"]
    if query.strip():
        wanted = _words(query)
        records = [r for r in records if wanted & _words(f"{r.get('subject')} {r.get('text')}") or query.lower() in str(r.get("text", "")).lower()]
    # Newest first; those of the same second, last saved first.
    return sorted(records[::-1], key=lambda r: str(r.get("created_at") or ""), reverse=True)


def get_memory(bot_id: str, memory_id: str, storage: Optional[StorageService] = None) -> Dict[str, Any]:
    record = next((r for r in _rows(bot_id, storage) if r.get("id") == memory_id), None)
    if record is None:
        raise MemoryError("There is no such memory.")
    return record


def _clean(subject: Any, text: Any) -> Tuple[str, str]:
    subject = " ".join(str(subject or "").split())[:SUBJECT_MAX]
    text = " ".join(str(text or "").split())[:TEXT_MAX]
    if not text:
        raise MemoryError("A memory needs its text.")
    return subject or "general", text


def looks_like_credential(text: str) -> bool:
    return bool(_CREDENTIAL.search(text) or _LOGIN_PAIR.search(text))


def add_memory(bot_id: str, subject: Any, text: Any, *, source: Optional[Dict[str, Any]] = None, replaces: Optional[str] = None,
               storage: Optional[StorageService] = None) -> Dict[str, Any]:
    storage = storage or current_storage()
    subject, text = _clean(subject, text)
    record = {
        "id": f"mem-{uuid.uuid4().hex[:10]}", "bot_id": bot_id, "subject": subject, "text": text, "created_at": _now(),
        "state": "current", "source": source or {}, **({"replaces": replaces} if replaces else {}),
    }
    _put(record, storage)
    _schedule_index(record)
    current = [r for r in _rows(bot_id, storage) if r.get("state") == "current"]
    for old in current[:max(0, len(current) - MAX_CURRENT)]:
        forget_memory(bot_id, old["id"], reason="the bot's memory was full", storage=storage)
    return record


def replace_memory(bot_id: str, memory_id: str, subject: Any, text: Any, *, source: Optional[Dict[str, Any]] = None,
                   storage: Optional[StorageService] = None) -> Dict[str, Any]:
    """A new memory that takes the old one's place; the old one stays in the history."""
    storage = storage or current_storage()
    old = get_memory(bot_id, memory_id, storage)
    new = add_memory(bot_id, subject or old.get("subject"), text, source=source, replaces=memory_id, storage=storage)
    old.update(state="replaced", replaced_by=new["id"], replaced_at=new["created_at"])
    _put(old, storage)
    _schedule_unindex(memory_id)
    return new


def forget_memory(bot_id: str, memory_id: str, *, reason: str = "", storage: Optional[StorageService] = None) -> Dict[str, Any]:
    storage = storage or current_storage()
    record = get_memory(bot_id, memory_id, storage)
    record.update(state="forgotten", forgotten_at=_now(), **({"forgotten_because": reason} if reason else {}))
    _put(record, storage)
    _schedule_unindex(memory_id)
    return record


def edit_memory(bot_id: str, memory_id: str, *, subject: Any = None, text: Any = None, storage: Optional[StorageService] = None) -> Dict[str, Any]:
    """The user's correction, in place."""
    storage = storage or current_storage()
    record = get_memory(bot_id, memory_id, storage)
    clean_subject, clean_text = _clean(subject if subject is not None else record.get("subject"), text if text is not None else record.get("text"))
    record.update(subject=clean_subject, text=clean_text, edited=_now())
    _put(record, storage)
    if record.get("state") == "current":
        _schedule_index(record)
    return record


def delete_memory(bot_id: str, memory_id: str, storage: Optional[StorageService] = None) -> None:
    """Gone for good (the user's delete), history and all."""
    storage = storage or current_storage()
    get_memory(bot_id, memory_id, storage)
    with storage.database.connect() as connection:
        connection.execute("DELETE FROM bot_memories WHERE owner_id = ? AND id = ?", (storage.owner_id, memory_id))
    _schedule_unindex(memory_id)


def delete_bot_memories(bot_id: str, storage: Optional[StorageService] = None) -> None:
    storage = storage or current_storage()
    ids = [r["id"] for r in _rows(bot_id, storage)]
    with storage.database.connect() as connection:
        connection.execute("DELETE FROM bot_memories WHERE owner_id = ? AND bot_id = ?", (storage.owner_id, bot_id))
    for memory_id in ids:
        _schedule_unindex(memory_id)


# ---- the search index ---------------------------------------------------------------------------


def _ref(record: Dict[str, Any]) -> str:
    return f"{record['bot_id']}:{record['id']}"


def _memory_text(record: Dict[str, Any]) -> str:
    return f"{record.get('subject') or ''}: {record.get('text') or ''}"


def _schedule(job) -> None:
    from app.services import retrieval

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
                await job(config)
            except Exception as exc:  # the memory itself is stored; a word match still finds it
                logger.warning("Memory index update failed: %s", exc)

    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        pass


def _schedule_index(record: Dict[str, Any]) -> None:
    from app.services import retrieval

    snapshot = dict(record)
    _schedule(lambda config: retrieval.index_text(KIND, _ref(snapshot), _memory_text(snapshot),
                                                  meta={"memory_id": snapshot["id"], "bot_id": snapshot["bot_id"], "name": f"memory: {snapshot.get('subject')}"}, config=config))


def _schedule_unindex(memory_id: str) -> None:
    from app.services import retrieval

    async def run(config) -> None:
        index = retrieval.index_for()
        with index.database.connect() as connection:
            connection.execute("DELETE FROM vectors WHERE owner_id = ? AND kind = ? AND ref LIKE ?", (index.owner_id, KIND, f"%:{memory_id}"))

    _schedule(run)


def _words(text: str) -> Set[str]:
    return {w.lower() for w in _WORD.findall(text or "") if len(w) > 2}


def _by_words(records: List[Dict[str, Any]], query: str, limit: int) -> List[Dict[str, Any]]:
    wanted = _words(query)
    scored = []
    for record in records:
        overlap = len(wanted & _words(_memory_text(record)))
        if overlap:
            scored.append((overlap, record))
    return [record for _, record in sorted(scored, key=lambda pair: -pair[0])[:limit]]


async def nearest(bot_id: str, query: str, *, limit: int, relevant_only: bool, storage: Optional[StorageService] = None) -> List[Dict[str, Any]]:
    """The bot's current memories closest to `query`: by the search index when there is one, else by words."""
    from app.services import retrieval

    current = {r["id"]: r for r in _rows(bot_id, storage) if r.get("state") == "current"}
    if not current or not (query or "").strip():
        return []
    try:
        config = retrieval.current_config()
        # The index only when every current memory is in it (one just kept may not be yet).
        if config.enabled and retrieval.index_for(storage).count(kind=KIND, ref_prefix=f"{bot_id}:") >= len(current):
            hits = await retrieval.retrieve(query, kind=KIND, ref_prefix=f"{bot_id}:", limit=limit, config=config, storage=storage, relevant_only=relevant_only)
            found = [current[h.meta.get("memory_id") or h.ref.split(":", 1)[-1]] for h in hits if (h.meta.get("memory_id") or h.ref.split(":", 1)[-1]) in current]
            if found or relevant_only:
                return found[:limit]
    except Exception as exc:
        logger.warning("Memory search failed for %s, matching words instead: %s", bot_id, exc)
    return _by_words(list(current.values()), query, limit)


def _line(record: Dict[str, Any]) -> str:
    return f"- [{record.get('subject')}] {record.get('text')} (learned {str(record.get('created_at') or '')[:10]})"


async def recall_context(bot_id: str, query: str, storage: Optional[StorageService] = None) -> str:
    """The bot's memories that match a message, as a block for the turn; "" when none."""
    if not enabled_for(bot_id, storage) and not list_memories(bot_id, storage=storage):
        return ""
    found = await nearest(bot_id, query, limit=RECALL_LIMIT, relevant_only=True, storage=storage)
    if not found:
        return ""
    return "Things you remember from earlier conversations that may bear on this message (automatic memory; newer than your notes where they differ):\n" + "\n".join(_line(r) for r in found)


# ---- whether a bot has it, and with which model -----------------------------------------------------


def _bot(bot_id: str, storage: Optional[StorageService] = None) -> Optional[Dict[str, Any]]:
    return next((b for b in (storage or current_storage()).get_bots() if b.get("id") == bot_id), None)


def enabled_for(bot_id: str, storage: Optional[StorageService] = None) -> bool:
    storage = storage or current_storage()
    bot = _bot(bot_id, storage)
    if bot is None:
        return False
    own = bot.get("auto_memory")
    if isinstance(own, bool):
        return own
    return bool(storage.get_settings().get(DEFAULT_SETTING))


def memory_model(bot: Dict[str, Any], config: Any, storage: StorageService) -> str:
    chosen = str(storage.get_settings().get(MODEL_SETTING) or "").strip()
    return chosen or config.default_model or str(bot.get("model") or "").strip()


# ---- capture: after a reply ------------------------------------------------------------------------


def _parse(text: str) -> List[Dict[str, Any]]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    braced = re.search(r"\{.*\}", text, re.S)
    for candidate in (text, braced.group(0) if braced else ""):
        if not candidate:
            continue
        try:
            data = json.loads(candidate, strict=False)
        except ValueError:
            continue
        items = data.get("memories") if isinstance(data, dict) else None
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    raise MemoryError("The memory model did not reply with the expected JSON.")


def build_prompt(*, notes: str, related: List[Dict[str, Any]], user_text: str, reply_text: str, today: str) -> str:
    numbered = "\n".join(f"{i + 1}. [{r.get('subject')}] {r.get('text')} (learned {str(r.get('created_at') or '')[:10]})" for i, r in enumerate(related)) or "(none)"
    notes = notes.strip()[:NOTES_CHARS] or "(none)"
    return (
        f"Today: {today}\n\n## The assistant's notes (already known; do not keep any of this again)\n{notes}\n\n"
        f"## Existing memories\n{numbered}\n\n## The exchange\nThe user wrote:\n{user_text.strip()[:EXCHANGE_CHARS]}\n\n"
        f"The assistant replied:\n{reply_text.strip()[:EXCHANGE_CHARS]}\n\nReply with the JSON only."
    )


def apply_operations(bot_id: str, operations: List[Dict[str, Any]], related: List[Dict[str, Any]], *, source: Dict[str, Any],
                     storage: Optional[StorageService] = None) -> Dict[str, int]:
    """Carry out what the model decided; anything malformed, unknown or credential-like is skipped."""
    done = {"added": 0, "replaced": 0, "forgotten": 0, "skipped": 0}
    by_number = {str(i + 1): r for i, r in enumerate(related)}
    touched: Set[str] = set()
    for op in operations[:20]:
        kind = str(op.get("op") or "add").lower()
        text = str(op.get("text") or "")
        target = by_number.get(str(op.get("id") or "").strip())
        try:
            if kind in ("add", "replace") and (not text.strip() or looks_like_credential(text)):
                done["skipped"] += 1
                continue
            if kind == "replace" and target is not None and target["id"] not in touched:
                replace_memory(bot_id, target["id"], op.get("subject"), text, source=source, storage=storage)
                touched.add(target["id"])
                done["replaced"] += 1
            elif kind == "forget" and target is not None and target["id"] not in touched:
                forget_memory(bot_id, target["id"], reason="the conversation showed it was wrong", storage=storage)
                touched.add(target["id"])
                done["forgotten"] += 1
            elif kind in ("add", "replace"):
                # A replace without a known target is a plain add.
                add_memory(bot_id, op.get("subject"), text, source=source, storage=storage)
                done["added"] += 1
            else:
                done["skipped"] += 1
        except MemoryError:
            done["skipped"] += 1
    return done


async def capture(thread_id: str, bot_id: str, user_text: str, reply_text: str, *, message_id: str = "",
                  storage: Optional[StorageService] = None, stream=None) -> Optional[Dict[str, int]]:
    """Read one exchange and keep what lasts. None when the bot does not have automatic memory on."""
    from app.services import llm_service, secrets_service

    storage = storage or current_storage()
    bot = _bot(bot_id, storage)
    if bot is None or not enabled_for(bot_id, storage):
        return None
    if len((user_text or "").strip()) + len((reply_text or "").strip()) < 40:
        return {"added": 0, "replaced": 0, "forgotten": 0, "skipped": 0}
    redact = secrets_service.redactor_for(bot_id, storage)
    user_text, reply_text = redact(user_text or ""), redact(reply_text or "")
    related = await nearest(bot_id, f"{user_text}\n{reply_text}"[:3000], limit=RELATED_FOR_EXTRACTION, relevant_only=False, storage=storage)
    prompt = build_prompt(notes=str(bot.get("memory") or ""), related=related, user_text=user_text, reply_text=reply_text, today=datetime.now().strftime("%Y-%m-%d (%A)"))
    config = dataclasses.replace(llm_service.current_llm_config(), reasoning_effort="none")
    model = memory_model(bot, config, storage)
    pieces: List[str] = []
    async for event in (stream or llm_service.stream_chat_completion)(
        model=model, messages=[{"role": "user", "content": prompt}], system_prompt=EXTRACTION_INSTRUCTIONS, config=config, tools=None,
    ):
        if event.get("type") == "content.delta":
            pieces.append(event.get("delta") or "")
        elif event.get("type") == "turn.completed" and not event.get("ok", True):
            raise MemoryError(event.get("error") or "The memory model failed.")
    operations = _parse("".join(pieces))
    return apply_operations(bot_id, operations, related, source={"thread_id": thread_id, "message_id": message_id}, storage=storage)


_locks: Dict[Tuple[str, str], asyncio.Lock] = {}


def schedule_capture(thread_id: str, bot_id: str, user_text: str, reply_text: str, *, message_id: str = "") -> None:
    """Capture in the background, one exchange at a time per bot; nothing when there is no event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    owner = get_current_owner()
    with owner_context(owner):
        if not enabled_for(bot_id):
            return
    lock = _locks.setdefault((owner, bot_id), asyncio.Lock())

    async def run() -> None:
        async with lock:
            with owner_context(owner):
                try:
                    outcome = await capture(thread_id, bot_id, user_text, reply_text, message_id=message_id)
                    if outcome and any(outcome[k] for k in ("added", "replaced", "forgotten")):
                        logger.info("Memory of %s: %s", bot_id, outcome)
                except Exception as exc:  # memory is a nicety; the reply is saved
                    logger.warning("Memory capture for %s failed: %s", bot_id, exc)

    loop.create_task(run())


# ---- the bot's tool ---------------------------------------------------------------------------------


async def execute_search(call: ActionInvocation) -> Dict[str, Any]:
    bot_id = str(call.arguments.get("bot_id") or "")
    query = str(call.arguments.get("query") or "").strip()
    if not query:
        raise MemoryError("Say what to look for.")
    found = await nearest(bot_id, query, limit=10, relevant_only=False)
    if not found:
        return {"memories": [], "note": "Nothing in your memory matches that."}
    return {"memories": [{"subject": r.get("subject"), "text": r.get("text"), "learned": str(r.get("created_at") or "")[:10]} for r in found]}


def register_memory_actions(gateway: Optional[ActionGateway] = None) -> None:
    (gateway or action_gateway).register_action(
        ActionDefinition(name=SEARCH_ACTION, tool="memory", action="search", intent="Search the bot's automatic memory.", risk="read", requires_approval=False),
        execute_search,
    )


register_memory_actions()
