"""Per-bot scheduled tasks: list, create, change, delete, run now."""

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import schedule_service
from app.services.schedule_service import MAX_NAME_CHARS, MAX_PROMPT_CHARS, ScheduleError
from app.services.scheduler import scheduler
from app.services.storage_service import storage_service

router = APIRouter(prefix="/api/v1/bots", tags=["schedules"])


class ScheduleInput(BaseModel):
    name: str = Field(max_length=MAX_NAME_CHARS)
    prompt: str = Field(max_length=MAX_PROMPT_CHARS)
    cadence: Dict[str, Any]


class ScheduleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=MAX_NAME_CHARS)
    prompt: Optional[str] = Field(default=None, max_length=MAX_PROMPT_CHARS)
    cadence: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None


def _ensure_bot(bot_id: str) -> None:
    if not any(bot.get("id") == bot_id for bot in storage_service.get_bots()):
        raise HTTPException(status_code=404, detail="Bot not found")


def _owned(bot_id: str, schedule_id: str) -> Dict[str, Any]:
    try:
        record = schedule_service.get_schedule(schedule_id)
    except ScheduleError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if record["bot_id"] != bot_id:
        raise HTTPException(status_code=404, detail="There is no schedule with that id.")
    return record


def _guard(action):
    try:
        return action()
    except ScheduleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{bot_id}/schedules")
async def list_schedules(bot_id: str):
    _ensure_bot(bot_id)
    now = schedule_service.now_local()
    return {
        "schedules": schedule_service.list_for_bot(bot_id),
        "now": now.strftime("%Y-%m-%d %H:%M"),
        "timezone": schedule_service.settings.SCHEDULE_TIMEZONE or "UTC",
    }


@router.post("/{bot_id}/schedules")
async def create_schedule(bot_id: str, body: ScheduleInput):
    _ensure_bot(bot_id)
    return {"schedule": _guard(lambda: schedule_service.create_schedule(bot_id, body.name, body.prompt, body.cadence, created_by="user"))}


@router.put("/{bot_id}/schedules/{schedule_id}")
async def update_schedule(bot_id: str, schedule_id: str, body: ScheduleUpdate):
    _ensure_bot(bot_id)
    _owned(bot_id, schedule_id)
    changes = body.model_dump(exclude_none=True)
    return {"schedule": _guard(lambda: schedule_service.update_schedule(schedule_id, **changes))}


@router.delete("/{bot_id}/schedules/{schedule_id}")
async def delete_schedule(bot_id: str, schedule_id: str):
    _ensure_bot(bot_id)
    _owned(bot_id, schedule_id)
    return {"status": "ok", "deleted": _guard(lambda: schedule_service.delete_schedule(schedule_id))}


@router.post("/{bot_id}/schedules/{schedule_id}/run")
async def run_schedule_now(bot_id: str, schedule_id: str):
    """Start this schedule's prompt now, as a run would; the cadence is unchanged."""
    _ensure_bot(bot_id)
    record = _owned(bot_id, schedule_id)
    started = await scheduler.run_now(record)
    if not started:
        raise HTTPException(status_code=409, detail="A reply is still in progress for this bot.")
    return {"status": "started", "schedule": schedule_service.public(schedule_service.get_schedule(schedule_id))}
