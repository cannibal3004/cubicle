"""A bot's own skills: how-tos it wrote itself, most often from what the user showed it.

These were called procedures, and are still kept under that name on the bot
record (`procedures`: id, title, description, steps), so they survive
compactions and conversations. They are skills like the library's (see
services/skills): every turn's tools prompt lists them, skill_read brings
one back in full and skill_save writes one; when the server has the
retrieval stack, the ones that match the message are retrieved into the
prompt as well. After a demonstration (services/demonstrations) the bot
writes the steps up in its own words and saves them here. The user sees,
edits and deletes them in the bot editor.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.services import retrieval
from app.services.identity import get_current_owner, owner_context
from app.services.storage_service import StorageService, current_storage

logger = logging.getLogger(__name__)

KIND = "procedure"
MAX_PROCEDURES = 60
TITLE_MAX = 120
DESCRIPTION_MAX = 300
STEPS_MAX = 12_000
_SPACES = re.compile(r"\s+")


class ProcedureError(RuntimeError):
    pass


def _ref(bot_id: str, procedure_id: str) -> str:
    return f"{bot_id}:{procedure_id}"


def clean_title(value: Any) -> str:
    if not isinstance(value, str):
        raise ProcedureError("A skill needs a name.")
    title = _SPACES.sub(" ", value).strip()
    if not title:
        raise ProcedureError("A skill needs a name.")
    return title[:TITLE_MAX]


def clean_description(value: Any) -> str:
    """When to use it, in a line (listed beside the title); may be empty."""
    return _SPACES.sub(" ", str(value or "")).strip()[:DESCRIPTION_MAX]


def clean_steps(value: Any) -> str:
    if not isinstance(value, str):
        raise ProcedureError("The instructions must be text.")
    steps = value.replace("\r\n", "\n").strip()
    if not steps:
        raise ProcedureError("The instructions cannot be empty.")
    if len(steps) > STEPS_MAX:
        raise ProcedureError(f"A skill you write is limited to {STEPS_MAX} characters; this is {len(steps)}. Keep it to what is needed to repeat the task.")
    return steps


def clean_procedures(value: Any) -> List[Dict[str, Any]]:
    """The editor's list, kept to the stored shape and limits."""
    if not isinstance(value, list):
        raise ProcedureError("procedures must be a list.")
    cleaned: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ProcedureError("Each procedure must be an object.")
        entry = {
            "id": str(item.get("id") or "").strip() or f"proc-{uuid.uuid4().hex[:8]}",
            "title": clean_title(item.get("title")),
            "steps": clean_steps(item.get("steps")),
            "description": clean_description(item.get("description")),
            "created_at": str(item.get("created_at") or datetime.now().isoformat()),
            "updated_at": str(item.get("updated_at") or datetime.now().isoformat()),
        }
        if isinstance(item.get("source"), dict):
            entry["source"] = {k: item["source"][k] for k in ("recording_id", "steps") if k in item["source"]}
        if any(e["id"] == entry["id"] for e in cleaned):
            raise ProcedureError("Skill ids must be unique.")
        cleaned.append(entry)
    if len(cleaned) > MAX_PROCEDURES:
        raise ProcedureError(f"A bot keeps at most {MAX_PROCEDURES} skills of its own.")
    return cleaned


def _bot(bots: List[Dict[str, Any]], bot_id: str) -> Dict[str, Any]:
    bot = next((b for b in bots if b.get("id") == bot_id), None)
    if bot is None:
        raise ProcedureError("The bot does not exist.")
    return bot


def list_procedures(bot_id: str, storage: Optional[StorageService] = None) -> List[Dict[str, Any]]:
    for bot in (storage or current_storage()).get_bots():
        if bot.get("id") == bot_id:
            return [dict(p) for p in (bot.get("procedures") or []) if isinstance(p, dict)]
    return []


def find_procedure(bot_id: str, title: str, storage: Optional[StorageService] = None) -> Optional[Dict[str, Any]]:
    """By title, case and spacing ignored; a unique prefix or substring of the title also finds it."""
    wanted = _SPACES.sub(" ", str(title or "")).strip().lower()
    if not wanted:
        return None
    procedures = list_procedures(bot_id, storage)
    exact = [p for p in procedures if str(p.get("title") or "").lower() == wanted]
    if exact:
        return exact[0]
    partial = [p for p in procedures if wanted in str(p.get("title") or "").lower()]
    return partial[0] if len(partial) == 1 else None


def save_procedure(
    bot_id: str, title: Any, steps: Any, *, description: Any = None, source: Optional[Dict[str, Any]] = None, storage: Optional[StorageService] = None
) -> Dict[str, Any]:
    """Add one of the bot's own skills, or replace the one with the same title. Returns it as stored."""
    storage = storage or current_storage()
    title = clean_title(title)
    steps = clean_steps(steps)
    description = clean_description(description)
    bots = storage.get_bots()
    bot = _bot(bots, bot_id)
    procedures = [p for p in (bot.get("procedures") or []) if isinstance(p, dict)]
    now = datetime.now().isoformat()
    existing = next((p for p in procedures if str(p.get("title") or "").lower() == title.lower()), None)
    if existing is not None:
        existing.update({"steps": steps, "updated_at": now})  # the title keeps its first spelling
        if description:
            existing["description"] = description
        if source:
            existing["source"] = dict(source)
        saved = existing
    else:
        if len(procedures) >= MAX_PROCEDURES:
            raise ProcedureError(f"This bot already keeps {MAX_PROCEDURES} skills of its own; delete one in the bot editor first.")
        saved = {"id": f"proc-{uuid.uuid4().hex[:8]}", "title": title, "description": description, "steps": steps, "created_at": now, "updated_at": now}
        if source:
            saved["source"] = dict(source)
        procedures.append(saved)
    bot["procedures"] = procedures
    storage.save_bots(bots)
    schedule_index(bot_id, saved)
    return dict(saved)


def delete_procedure(bot_id: str, procedure_id: str, storage: Optional[StorageService] = None) -> bool:
    storage = storage or current_storage()
    bots = storage.get_bots()
    bot = _bot(bots, bot_id)
    before = [p for p in (bot.get("procedures") or []) if isinstance(p, dict)]
    after = [p for p in before if p.get("id") != procedure_id]
    if len(after) == len(before):
        return False
    bot["procedures"] = after
    storage.save_bots(bots)
    schedule_unindex(bot_id, procedure_id)
    return True


def sync_index(bot_id: str, before: List[Dict[str, Any]], after: List[Dict[str, Any]]) -> None:
    """After the editor replaced the list: unindex what went, index what is new or changed."""
    old = {str(p.get("id")): p for p in before if isinstance(p, dict)}
    new = {str(p.get("id")): p for p in after if isinstance(p, dict)}
    for procedure_id in old.keys() - new.keys():
        schedule_unindex(bot_id, procedure_id)
    for procedure_id, procedure in new.items():
        previous = old.get(procedure_id)
        if previous is None or previous.get("title") != procedure.get("title") or previous.get("steps") != procedure.get("steps"):
            schedule_index(bot_id, procedure)


# ---- retrieval ------------------------------------------------------------------------------


def procedure_text(procedure: Dict[str, Any]) -> str:
    return f"{procedure.get('title') or ''}\n{procedure.get('steps') or ''}".strip()


async def index_procedure(bot_id: str, procedure: Dict[str, Any], *, config: Optional[retrieval.RetrievalConfig] = None, storage=None, embed=None) -> int:
    meta = {"name": f"procedure: {procedure.get('title') or ''}", "bot_id": bot_id, "procedure_id": procedure.get("id"), "title": procedure.get("title")}
    return await retrieval.index_text(KIND, _ref(bot_id, str(procedure.get("id"))), procedure_text(procedure), meta=meta, config=config, storage=storage, embed=embed)


def _schedule(coro_factory) -> None:
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
                await coro_factory(config)
            except Exception as exc:
                logger.warning("Procedure index update failed: %s", exc)

    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        pass


def schedule_index(bot_id: str, procedure: Dict[str, Any]) -> None:
    _schedule(lambda config: index_procedure(bot_id, procedure, config=config))


def schedule_unindex(bot_id: str, procedure_id: str) -> None:
    async def run(config) -> None:
        retrieval.index_for().delete(KIND, _ref(bot_id, procedure_id))

    _schedule(run)


async def procedure_context(bot_id: str, query: str, *, limit: int = 2, config: Optional[retrieval.RetrievalConfig] = None, storage=None, embed=None) -> str:
    """The stored procedures that match the message, in full, as a prompt block; "" when none."""
    config = config or retrieval.current_config()
    if not config.enabled or not (query or "").strip():
        return ""
    try:
        hits = await retrieval.retrieve(query, kind=KIND, ref_prefix=f"{bot_id}:", limit=limit, config=config, storage=storage, embed=embed, relevant_only=True)
    except Exception as exc:
        logger.warning("Procedure retrieval failed for %s: %s", bot_id, exc)
        return ""
    if not hits:
        return ""
    by_id = {str(p.get("id")): p for p in list_procedures(bot_id, storage)}
    lines = ["Skills you wrote (most from what the user showed you) that match this message; follow one when the task is the same (the whole skill is given):"]
    seen = set()
    for hit in hits:
        procedure_id = str(hit.meta.get("procedure_id") or hit.ref.split(":", 1)[-1])
        if procedure_id in seen:
            continue
        seen.add(procedure_id)
        procedure = by_id.get(procedure_id)
        if not procedure:
            continue
        lines.append(f"--- {procedure.get('title')} ---\n{str(procedure.get('steps') or '').strip()}")
    return "\n".join(lines) if len(lines) > 1 else ""
