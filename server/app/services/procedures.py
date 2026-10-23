"""Procedures: how-tos a bot keeps, written from what the user showed it.

After a demonstration (services/demonstrations) the bot writes the steps up
in its own words and saves them here, on the bot record (`procedures`), so
they survive compactions and conversations. Every turn's tools prompt lists
the titles, so the bot knows what it has been shown; procedure_read brings
one back in full, and when the server has the retrieval stack, the
procedures that match the message are retrieved into the prompt as well.
The user sees and deletes them in the bot editor.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.services import retrieval
from app.services.action_gateway import ActionDefinition, ActionGateway, ActionInvocation, action_gateway
from app.services.identity import get_current_owner, owner_context
from app.services.storage_service import StorageService, current_storage

logger = logging.getLogger(__name__)

KIND = "procedure"
MAX_PROCEDURES = 60
TITLE_MAX = 120
STEPS_MAX = 12_000
SAVE_ACTION = "procedure.save"
READ_ACTION = "procedure.read"
_SPACES = re.compile(r"\s+")


class ProcedureError(RuntimeError):
    pass


def _ref(bot_id: str, procedure_id: str) -> str:
    return f"{bot_id}:{procedure_id}"


def clean_title(value: Any) -> str:
    if not isinstance(value, str):
        raise ProcedureError("A procedure needs a title.")
    title = _SPACES.sub(" ", value).strip()
    if not title:
        raise ProcedureError("A procedure needs a title.")
    return title[:TITLE_MAX]


def clean_steps(value: Any) -> str:
    if not isinstance(value, str):
        raise ProcedureError("The steps must be text.")
    steps = value.replace("\r\n", "\n").strip()
    if not steps:
        raise ProcedureError("The steps cannot be empty.")
    if len(steps) > STEPS_MAX:
        raise ProcedureError(f"The steps are limited to {STEPS_MAX} characters; this is {len(steps)}. Keep the procedure to what is needed to repeat the task.")
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
            "created_at": str(item.get("created_at") or datetime.now().isoformat()),
            "updated_at": str(item.get("updated_at") or datetime.now().isoformat()),
        }
        if isinstance(item.get("source"), dict):
            entry["source"] = {k: item["source"][k] for k in ("recording_id", "steps") if k in item["source"]}
        if any(e["id"] == entry["id"] for e in cleaned):
            raise ProcedureError("Procedure ids must be unique.")
        cleaned.append(entry)
    if len(cleaned) > MAX_PROCEDURES:
        raise ProcedureError(f"A bot keeps at most {MAX_PROCEDURES} procedures.")
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


def save_procedure(bot_id: str, title: Any, steps: Any, *, source: Optional[Dict[str, Any]] = None, storage: Optional[StorageService] = None) -> Dict[str, Any]:
    """Add a procedure, or replace the one with the same title. Returns the stored procedure."""
    storage = storage or current_storage()
    title = clean_title(title)
    steps = clean_steps(steps)
    bots = storage.get_bots()
    bot = _bot(bots, bot_id)
    procedures = [p for p in (bot.get("procedures") or []) if isinstance(p, dict)]
    now = datetime.now().isoformat()
    existing = next((p for p in procedures if str(p.get("title") or "").lower() == title.lower()), None)
    if existing is not None:
        existing.update({"steps": steps, "updated_at": now})  # the title keeps its first spelling
        if source:
            existing["source"] = dict(source)
        saved = existing
    else:
        if len(procedures) >= MAX_PROCEDURES:
            raise ProcedureError(f"This bot already keeps {MAX_PROCEDURES} procedures; delete one in the bot editor first.")
        saved = {"id": f"proc-{uuid.uuid4().hex[:8]}", "title": title, "steps": steps, "created_at": now, "updated_at": now}
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
        hits = await retrieval.retrieve(query, kind=KIND, ref_prefix=f"{bot_id}:", limit=limit, config=config, storage=storage, embed=embed)
    except Exception as exc:
        logger.warning("Procedure retrieval failed for %s: %s", bot_id, exc)
        return ""
    if not hits:
        return ""
    by_id = {str(p.get("id")): p for p in list_procedures(bot_id, storage)}
    lines = ["Procedures the user showed you earlier that match this message (follow one when the task is the same; the whole procedure is given):"]
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


# ---- prompt and tools -------------------------------------------------------------------------


def prompt_block(bot: Optional[Dict[str, Any]]) -> str:
    """The titles, for the tools prompt; "" when the bot has none."""
    procedures = [p for p in ((bot or {}).get("procedures") or []) if isinstance(p, dict) and p.get("title")]
    if not procedures:
        return ""
    titles = "; ".join(f'"{p["title"]}"' for p in procedures[:MAX_PROCEDURES])
    return (
        f"- Procedures the user has shown you ({len(procedures)}): {titles}. When a task matches one, read it with procedure_read first "
        "and follow it; when you learn that a step has changed, save the corrected procedure under the same title."
    )


def _bot_id(call: ActionInvocation) -> str:
    bot_id = call.arguments.get("bot_id")
    if not isinstance(bot_id, str) or not bot_id.strip():
        raise ProcedureError("Procedure actions require a bot id.")
    return bot_id


async def execute_save(call: ActionInvocation) -> Dict[str, Any]:
    bot_id = _bot_id(call)
    saved = save_procedure(bot_id, call.arguments.get("title"), call.arguments.get("steps"), source=call.arguments.get("source"))
    return {"saved": True, "id": saved["id"], "title": saved["title"], "chars": len(saved["steps"]), "count": len(list_procedures(bot_id))}


async def execute_read(call: ActionInvocation) -> Dict[str, Any]:
    bot_id = _bot_id(call)
    title = call.arguments.get("title")
    procedure = find_procedure(bot_id, str(title or ""))
    if procedure is None:
        titles = [p.get("title") for p in list_procedures(bot_id)]
        raise ProcedureError(f"No procedure called {str(title)!r}. The titles are: {titles}" if titles else "This bot has no procedures yet.")
    return {"id": procedure["id"], "title": procedure["title"], "steps": procedure["steps"], "updated_at": procedure.get("updated_at")}


def register_procedure_actions(gateway: Optional[ActionGateway] = None) -> None:
    target = gateway or action_gateway
    target.register_action(
        ActionDefinition(name=SAVE_ACTION, tool="procedure", action="save", intent="Keep a procedure the bot was shown.", risk="write", requires_approval=False),
        execute_save,
    )
    target.register_action(
        ActionDefinition(name=READ_ACTION, tool="procedure", action="read", intent="Show the bot one of its procedures.", risk="read", requires_approval=False),
        execute_read,
    )


register_procedure_actions()
