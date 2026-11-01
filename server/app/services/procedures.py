"""A bot's own skills: how-tos it wrote itself, most often from what the user showed it.

These were called procedures, and are still kept under that name on the bot
record (`procedures`: id, title, description, steps), so they survive
compactions and conversations. A skill can have files besides its
instructions (scripts, templates, references), like a library skill: they
are kept on disk under DATA_DIR/skills/<owner>/bots/<bot id>/<skill id>/,
listed on the record as `files`, and `version` changes with every save so
the copy in the bot's computer is refreshed. They go wherever the bot goes:
a teammate using a shared bot gets them too, and no one else does. They are skills like the library's (see
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
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services import retrieval
from app.services.identity import get_current_owner, owner_context
from app.services.storage_service import StorageService, current_storage

logger = logging.getLogger(__name__)

KIND = "procedure"
MAX_PROCEDURES = 60
TITLE_MAX = 120
DESCRIPTION_MAX = 300
STEPS_MAX = 12_000  # what a bot writes itself
IMPORTED_STEPS_MAX = 60_000  # a skill installed into the bot or copied from the library
SOURCE_KEYS = ("recording_id", "steps", "kind", "url", "name", "path", "skill_id")
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


def clean_steps(value: Any, limit: int = STEPS_MAX) -> str:
    if not isinstance(value, str):
        raise ProcedureError("The instructions must be text.")
    steps = value.replace("\r\n", "\n").strip()
    if not steps:
        raise ProcedureError("The instructions cannot be empty.")
    if len(steps) > limit:
        if limit == STEPS_MAX:
            raise ProcedureError(f"A skill you write is limited to {STEPS_MAX} characters; this is {len(steps)}. Keep it to what is needed to repeat "
                                 "the task, and put long reference material in files of the skill (skill_save with a folder).")
        raise ProcedureError(f"A bot's own skill is limited to {limit} characters of instructions; this one has {len(steps)}. Install it in the skills library instead.")
    return steps


def clean_procedures(value: Any, existing: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """The editor's list, kept to the stored shape and limits. A skill's files are the server's
    to know: they are carried over from the stored skill of the same id, never taken from the list."""
    if not isinstance(value, list):
        raise ProcedureError("procedures must be a list.")
    stored = {str(p.get("id")): p for p in existing or [] if isinstance(p, dict)}
    cleaned: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ProcedureError("Each procedure must be an object.")
        entry = {
            "id": str(item.get("id") or "").strip() or f"proc-{uuid.uuid4().hex[:8]}",
            "title": clean_title(item.get("title")),
            "steps": clean_steps(item.get("steps"), IMPORTED_STEPS_MAX),
            "description": clean_description(item.get("description")),
            "created_at": str(item.get("created_at") or datetime.now().isoformat()),
            "updated_at": str(item.get("updated_at") or datetime.now().isoformat()),
        }
        if isinstance(item.get("source"), dict):
            entry["source"] = {k: item["source"][k] for k in SOURCE_KEYS if k in item["source"]}
        before = stored.get(entry["id"]) or {}
        for key in ("files", "version"):
            if before.get(key):
                entry[key] = before[key]
        if before and any(before.get(k) != entry[k] for k in ("steps", "title", "description")):
            entry["version"] = uuid.uuid4().hex[:8]  # the copy in the bot's computer is refreshed
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
    bot_id: str, title: Any, steps: Any, *, description: Any = None, source: Optional[Dict[str, Any]] = None,
    files: Optional[Dict[str, bytes]] = None, limit: int = STEPS_MAX, storage: Optional[StorageService] = None
) -> Dict[str, Any]:
    """Add one of the bot's own skills, or replace the one with the same title. Returns it as stored.

    `files` (by path; a SKILL.md among them is left out) replaces the skill's other files; None keeps
    the ones it has."""
    storage = storage or current_storage()
    title = clean_title(title)
    steps = clean_steps(steps, limit)
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
            existing["source"] = {k: source[k] for k in SOURCE_KEYS if k in source}
        saved = existing
    else:
        if len(procedures) >= MAX_PROCEDURES:
            raise ProcedureError(f"This bot already keeps {MAX_PROCEDURES} skills of its own; delete one in the bot editor first.")
        saved = {"id": f"proc-{uuid.uuid4().hex[:8]}", "title": title, "description": description, "steps": steps, "created_at": now, "updated_at": now}
        if source:
            saved["source"] = {k: source[k] for k in SOURCE_KEYS if k in source}
        procedures.append(saved)
    if files is not None:
        listing = write_files(bot_id, saved["id"], files, storage)
        if listing:
            saved["files"] = listing
        else:
            saved.pop("files", None)
    saved["version"] = uuid.uuid4().hex[:8]
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
    remove_files(bot_id, procedure_id, storage)
    return True


# ---- a skill's files ---------------------------------------------------------------------------

MAX_FILES = 300
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_SKILL_BYTES = 25 * 1024 * 1024
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def bot_folder(bot_id: str, storage: Optional[StorageService] = None) -> Path:
    storage = storage or current_storage()
    return Path(storage.data_dir) / "skills" / _UNSAFE.sub("-", storage.owner_id) / "bots" / _UNSAFE.sub("-", bot_id)


def folder(bot_id: str, procedure_id: str, storage: Optional[StorageService] = None) -> Path:
    return bot_folder(bot_id, storage) / _UNSAFE.sub("-", procedure_id)


def write_files(bot_id: str, procedure_id: str, files: Dict[str, bytes], storage: Optional[StorageService] = None) -> List[Dict[str, Any]]:
    """The skill's files (SKILL.md left out), swapped in whole; their listing ([] when none)."""
    files = {path: data for path, data in files.items() if path.lower() != "skill.md"}
    if len(files) > MAX_FILES:
        raise ProcedureError(f"A skill has at most {MAX_FILES} files.")
    if any(len(data) > MAX_FILE_BYTES for data in files.values()) or sum(len(d) for d in files.values()) > MAX_SKILL_BYTES:
        raise ProcedureError(f"A skill's files are limited to {MAX_FILE_BYTES // (1024 * 1024)} MB each and {MAX_SKILL_BYTES // (1024 * 1024)} MB in all.")
    target = folder(bot_id, procedure_id, storage)
    if not files:
        shutil.rmtree(target, ignore_errors=True)
        return []
    staging = target.with_name(f"{target.name}.new-{uuid.uuid4().hex[:6]}")
    try:
        for path, data in files.items():
            destination = (staging / path).resolve()
            if staging.resolve() not in destination.parents:
                raise ProcedureError(f"{path} is not a path inside the skill.")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
    except (OSError, ProcedureError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(exc, ProcedureError):
            raise
        raise ProcedureError(f"Could not store the skill's files: {exc}") from exc
    return [{"path": path, "size": len(data)} for path, data in sorted(files.items())]


def files_of(bot_id: str, procedure: Dict[str, Any], storage: Optional[StorageService] = None) -> Dict[str, bytes]:
    """The skill's files besides its instructions, by path."""
    base = folder(bot_id, str(procedure.get("id") or ""), storage)
    if not procedure.get("files") or not base.is_dir():
        return {}
    return {path.relative_to(base).as_posix(): path.read_bytes() for path in base.rglob("*") if path.is_file()}


def remove_files(bot_id: str, procedure_id: str, storage: Optional[StorageService] = None) -> None:
    shutil.rmtree(folder(bot_id, procedure_id, storage), ignore_errors=True)


def remove_all_files(bot_id: str, storage: Optional[StorageService] = None) -> None:
    shutil.rmtree(bot_folder(bot_id, storage), ignore_errors=True)


def sync_files(bot_id: str, before: List[Dict[str, Any]], after: List[Dict[str, Any]]) -> None:
    """After the editor replaced the list: the files of skills that went go too."""
    kept = {str(p.get("id")) for p in after if isinstance(p, dict)}
    for procedure in before:
        if isinstance(procedure, dict) and str(procedure.get("id")) not in kept:
            remove_files(bot_id, str(procedure.get("id")))


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
