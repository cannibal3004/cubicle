"""A bot's own skills, with files: install into one bot (a link, an upload, or a copy of a library
skill), and read one of a skill's files. Listing, editing and deleting go through the bot itself
(`procedures`); see services/skills and services/procedures."""

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.services import procedures, skills

router = APIRouter(prefix="/api/v1/bots", tags=["skills"])


class InstallInput(BaseModel):
    url: str = Field(max_length=1000)


class FromLibraryInput(BaseModel):
    skill_id: str = Field(max_length=100)


def _guard(action):
    try:
        return action()
    except skills.SkillError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _with_bot(bot_id: str, installed):
    from app.routers.bots import get_bot

    return {"installed": installed, "bot": await get_bot(bot_id)}


@router.post("/{bot_id}/skills/install")
async def install(bot_id: str, body: InstallInput):
    """From a link (a GitHub folder or repository, a .zip, a SKILL.md) into this bot alone."""
    _guard(lambda: skills._bot(bot_id))
    try:
        installed = await skills.install_from_url(body.url, skills.BotSkillStore(bot_id))
    except skills.SkillError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _with_bot(bot_id, installed)


@router.post("/{bot_id}/skills/upload")
async def upload(bot_id: str, file: UploadFile = File(...)):
    """A zip of a skill folder (or a few), or a lone SKILL.md, into this bot alone."""
    _guard(lambda: skills._bot(bot_id))
    data = await file.read(skills.MAX_DOWNLOAD_BYTES + 1)
    if len(data) > skills.MAX_DOWNLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"The upload is larger than {skills.MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB.")
    installed = _guard(lambda: skills.install_upload(file.filename or "", data, skills.BotSkillStore(bot_id)))
    return await _with_bot(bot_id, installed)


@router.post("/{bot_id}/skills/from-library")
async def from_library(bot_id: str, body: FromLibraryInput):
    """A copy of a library skill, files and all, as this bot's own (it then wins over the library's)."""
    return await _with_bot(bot_id, [_guard(lambda: skills.copy_from_library(bot_id, body.skill_id))])


@router.get("/{bot_id}/skills/{skill_id}/files/{path:path}")
async def read_file(bot_id: str, skill_id: str, path: str):
    own = next((p for p in procedures.list_procedures(bot_id) if p.get("id") == skill_id), None)
    if own is None:
        raise HTTPException(status_code=404, detail="That bot has no such skill of its own.")
    return {"path": path, "text": _guard(lambda: skills.read_bot_file(bot_id, own, path))}
