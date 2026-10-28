"""A bot's projects: list, create, rename, archive, delete, their work notes and their log."""

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import projects
from app.services.projects import NAME_MAX_CHARS, WORK_NOTES_MAX_CHARS, ProjectError
from app.services.storage_service import current_storage

router = APIRouter(prefix="/api/v1", tags=["projects"])


class ProjectInput(BaseModel):
    name: str = Field(max_length=NAME_MAX_CHARS * 2)
    notes: str = Field(default="", max_length=WORK_NOTES_MAX_CHARS)


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=NAME_MAX_CHARS * 2)
    notes: Optional[str] = Field(default=None, max_length=WORK_NOTES_MAX_CHARS)
    archived: Optional[bool] = None


def _ensure_bot(bot_id: str) -> None:
    if not any(bot.get("id") == bot_id for bot in current_storage().get_bots()):
        raise HTTPException(status_code=404, detail="Bot not found")


def _project(project_id: str) -> Dict[str, Any]:
    record = projects.get(project_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return record


def summary(record: Dict[str, Any]) -> Dict[str, Any]:
    storage = current_storage()
    return {
        "id": record["id"],
        "bot_id": record["bot_id"],
        "name": record.get("name") or "",
        "notes": str(record.get("notes") or ""),
        "notes_updated_at": record.get("notes_updated_at"),
        "notes_limit": WORK_NOTES_MAX_CHARS,
        "archived": bool(record.get("archived")),
        "created_at": record.get("created_at"),
        "log_entries": storage.count_log(record["id"]),
        "log_updated_at": record.get("log_updated_at"),
        "conversations": [c["id"] for c in projects.conversations_in(record["id"], storage)],
    }


@router.get("/bots/{bot_id}/projects")
async def list_projects(bot_id: str):
    _ensure_bot(bot_id)
    return [summary(p) for p in projects.list_for_bot(bot_id)]


@router.post("/bots/{bot_id}/projects")
async def create_project(bot_id: str, body: ProjectInput):
    _ensure_bot(bot_id)
    try:
        return summary(projects.create(bot_id, body.name, body.notes))
    except ProjectError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/projects/{project_id}")
async def get_project(project_id: str):
    return summary(_project(project_id))


@router.patch("/projects/{project_id}")
async def update_project(project_id: str, body: ProjectUpdate):
    record = _project(project_id)
    changes: Dict[str, Any] = {}
    if body.name is not None:
        changes["name"] = body.name
    if body.archived is not None:
        changes["archived"] = body.archived
    edited = None
    if body.notes is not None:
        try:
            notes = projects.clean_work_notes(body.notes)
        except ProjectError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if notes != str(record.get("notes") or ""):
            changes["notes"] = notes
            edited = notes
    try:
        record = projects.update(project_id, changes) if changes else record
    except ProjectError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if edited is not None:
        # The project's conversations see the user's edit where it happened.
        storage = current_storage()
        activity = storage.thread_activity()
        for conversation in projects.conversations_in(project_id, storage):
            if conversation.get("archived") or not (activity.get(conversation["id"]) or {}).get("messages"):
                continue
            storage.add_message(projects.work_note_message(conversation["id"], edited, bot_id=record["bot_id"], by="user", project_name=record["name"]))
            projects.seen(conversation["id"], storage)
    return summary(record)


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str):
    """Delete a project and its log; its conversations stay, outside any project."""
    _project(project_id)
    moved = projects.delete(project_id)
    return {"status": "ok", "deleted_id": project_id, "conversations_moved_out": moved}


@router.get("/projects/{project_id}/log")
async def project_log(project_id: str, limit: int = 100):
    _project(project_id)
    entries = current_storage().get_log(project_id, limit=max(1, min(limit, 1000)))
    return {"entries": list(reversed(entries))}  # newest first, as the panel shows them
