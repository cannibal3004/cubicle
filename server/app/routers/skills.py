"""The skills library: list, install (a link or an upload), write, read, change, remove."""

from typing import Any, Dict, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from app.services import skills
from app.services.identity import is_admin

router = APIRouter(prefix="/api/v1/skills", tags=["skills"])

MAX_UPLOAD_BYTES = skills.MAX_DOWNLOAD_BYTES


class InstallInput(BaseModel):
    url: str = Field(max_length=1000)
    # system: every bot of every account (admins only); user: the caller's bots.
    scope: str = "user"


class SkillInput(BaseModel):
    name: str = Field(max_length=skills.NAME_MAX)
    description: str = Field(max_length=skills.DESCRIPTION_MAX)
    instructions: str = Field(max_length=skills.INSTRUCTIONS_MAX)
    scope: str = "user"


class SkillUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=skills.NAME_MAX)
    description: Optional[str] = Field(default=None, max_length=skills.DESCRIPTION_MAX)
    instructions: Optional[str] = Field(default=None, max_length=skills.INSTRUCTIONS_MAX)


def _guard(action):
    try:
        return action()
    except skills.SkillError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _store(request: Request, scope: str) -> skills.SkillStore:
    if scope == "system":
        if not is_admin(getattr(request.state, "user", None)):
            raise HTTPException(status_code=403, detail="Only an admin can change system skills.")
        return skills.system_skill_store
    if scope != "user":
        raise HTTPException(status_code=422, detail="scope must be system or user.")
    return skills.user_skill_store


def _record(request: Request, skill_id: str) -> Dict[str, Any]:
    """A skill the caller may change: their own, or a system one if admin."""
    record = _guard(lambda: skills.find(skill_id))
    _store(request, record.get("scope", "user"))
    return record


@router.get("")
async def list_skills():
    """System skills and the caller's own, each with its scope."""
    return {"skills": skills.library()}


@router.post("/install")
async def install(body: InstallInput, request: Request):
    """From a link: a GitHub repository or folder, a .zip, or a SKILL.md. A repository with several
    skills installs each; one already installed under the same name is replaced."""
    store = _store(request, body.scope)
    try:
        installed = await skills.install_from_url(body.url, store)
    except skills.SkillError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"installed": installed}


@router.post("/upload")
async def upload(request: Request, file: UploadFile = File(...), scope: str = Form("user")):
    """A zip of one or more skill folders, or a lone SKILL.md."""
    store = _store(request, scope)
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"The upload is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    return {"installed": _guard(lambda: skills.install_upload(file.filename or "", data, store))}


@router.post("")
async def create(body: SkillInput, request: Request):
    """A skill written here: name, description and instructions (a SKILL.md and nothing else)."""
    store = _store(request, body.scope)
    return {"skill": _guard(lambda: store.create(body.name, body.description, body.instructions))}


class FromBotInput(BaseModel):
    bot_id: str = Field(max_length=100)
    skill_id: str = Field(max_length=100)


@router.post("/from-bot")
async def move_from_bot(body: FromBotInput):
    """One of a bot's own skills moved into the caller's library: every bot gets it; the bot's copy goes."""
    from app.routers.bots import get_bot

    outcome = _guard(lambda: skills.move_from_bot(body.bot_id, body.skill_id))
    return {"skill": outcome["skill"], "bot": await get_bot(body.bot_id)}


@router.get("/{skill_id}")
async def read(skill_id: str):
    """One skill with its instructions (SKILL.md without its front matter)."""
    record = _guard(lambda: skills.find(skill_id))
    return {"skill": record, "instructions": _guard(lambda: skills.store_for(record).instructions(record))}


@router.get("/{skill_id}/files/{path:path}")
async def read_file(skill_id: str, path: str):
    record = _guard(lambda: skills.find(skill_id))
    return {"path": path, "text": _guard(lambda: skills.store_for(record).read_file(record, path))}


@router.put("/{skill_id}")
async def update(skill_id: str, body: SkillUpdate, request: Request):
    record = _record(request, skill_id)
    store = skills.store_for(record)
    return {"skill": _guard(lambda: store.update(skill_id, name=body.name, description=body.description, instructions=body.instructions))}


@router.delete("/{skill_id}")
async def remove(skill_id: str, request: Request):
    record = _record(request, skill_id)
    return {"removed": _guard(lambda: skills.store_for(record).remove(skill_id))}
