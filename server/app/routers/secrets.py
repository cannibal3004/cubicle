"""Named secrets: app-wide and per bot. Names and timestamps out, values in only."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import secrets_service
from app.services.secrets_service import MAX_VALUE_CHARS, SecretError
from app.services.storage_service import storage_service

router = APIRouter(prefix="/api/v1", tags=["secrets"])


class SecretValue(BaseModel):
    value: str = Field(max_length=MAX_VALUE_CHARS)


def _ensure_bot(bot_id: str) -> None:
    if not any(bot.get("id") == bot_id for bot in storage_service.get_bots()):
        raise HTTPException(status_code=404, detail="Bot not found")


def _guard(action):
    try:
        return action()
    except SecretError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/secrets")
async def list_app_secrets():
    return {"secrets": secrets_service.list_app_secrets()}


@router.put("/secrets/{name}")
async def set_app_secret(name: str, body: SecretValue):
    return _guard(lambda: secrets_service.set_secret("app", "", name, body.value))


@router.delete("/secrets/{name}")
async def delete_app_secret(name: str):
    if not _guard(lambda: secrets_service.delete_secret("app", "", name)):
        raise HTTPException(status_code=404, detail="Secret not found")
    return {"status": "ok", "name": name.upper()}


@router.get("/bots/{bot_id}/secrets")
async def list_bot_secrets(bot_id: str):
    _ensure_bot(bot_id)
    return {"secrets": secrets_service.list_for_bot(bot_id)}


@router.put("/bots/{bot_id}/secrets/{name}")
async def set_bot_secret(bot_id: str, name: str, body: SecretValue):
    _ensure_bot(bot_id)
    return _guard(lambda: secrets_service.set_secret("bot", bot_id, name, body.value))


@router.delete("/bots/{bot_id}/secrets/{name}")
async def delete_bot_secret(bot_id: str, name: str):
    _ensure_bot(bot_id)
    if not _guard(lambda: secrets_service.delete_secret("bot", bot_id, name)):
        raise HTTPException(status_code=404, detail="Secret not found")
    return {"status": "ok", "name": name.upper()}
