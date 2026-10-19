"""Bot personas: create, edit, archive, and delete."""

import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.schemas.contracts import Bot
from app.services import llm_service
from app.services.bot_memory import MEMORY_MAX_CHARS, MemoryError, clean_memory, memory_note_message
from app.services.bot_todo import TODO_MAX_ITEMS, TodoError, clean_todo, todo_note_message
from app.services.computer_provider import ComputerProviderError, computer_id_for_bot, computer_provider
from app.services.orchestrator import ensure_orchestrator
from app.services.storage_service import current_storage
from app.services.turn_manager import turn_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/bots", tags=["bots"])

MAX_NAME = 80
MAX_SHORT = 200
MAX_PROMPT = 20_000
# Only these fields can be changed from the client; ids, counters, and
# timestamps are the server's.
EDITABLE_FIELDS = {"name", "role", "description", "avatar", "model", "accent_color", "system_prompt", "tools", "pinned", "tool_settings", "auto_approval", "gate_threshold", "trusted_tools"}  # memory is handled separately
MAX_TOOL_SETTINGS_ENTRIES = 2000
MAX_TRUSTED_TOOLS = 500
_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def clean_trusted_tools(value: Any) -> List[str]:
    """A bounded list of tool names, each once, in the order given."""
    if not isinstance(value, list):
        raise HTTPException(status_code=422, detail="trusted_tools must be a list of tool names.")
    cleaned: List[str] = []
    for name in value:
        if not isinstance(name, str) or not _TOOL_NAME.match(name):
            raise HTTPException(status_code=422, detail="trusted_tools must be a list of tool names.")
        if name not in cleaned:
            cleaned.append(name)
    if len(cleaned) > MAX_TRUSTED_TOOLS:
        raise HTTPException(status_code=422, detail="trusted_tools has too many entries.")
    return cleaned


def clean_tool_settings(value: Any) -> Dict[str, Dict[str, bool]]:
    """Keep only {"groups"|"toolkits"|"tools": {str: bool}} with bounded size."""
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="tool_settings must be an object.")
    cleaned: Dict[str, Dict[str, bool]] = {}
    total = 0
    for section in ("groups", "toolkits", "tools"):
        entries = value.get(section)
        if entries is None:
            continue
        if not isinstance(entries, dict):
            raise HTTPException(status_code=422, detail=f"tool_settings.{section} must be an object.")
        kept = {}
        for key, flag in entries.items():
            if not isinstance(key, str) or not isinstance(flag, bool) or len(key) > 128:
                raise HTTPException(status_code=422, detail=f"tool_settings.{section} must map names to booleans.")
            # Only disabled entries need storing; enabled is the default.
            if flag is False:
                kept[key] = False
        total += len(kept)
        if kept:
            cleaned[section] = kept
    if total > MAX_TOOL_SETTINGS_ENTRIES:
        raise HTTPException(status_code=422, detail="tool_settings has too many entries.")
    return cleaned


class BotInput(BaseModel):
    name: str = Field(default="", max_length=MAX_NAME)
    role: str = Field(default="", max_length=MAX_SHORT)
    description: str = Field(default="", max_length=MAX_SHORT)
    avatar: str = Field(default="", max_length=16)
    model: Optional[str] = Field(default=None, max_length=200)
    accent_color: str = Field(default="", max_length=32)
    system_prompt: str = Field(default="", max_length=MAX_PROMPT)
    tools: List[str] = Field(default_factory=list)
    trusted_tools: List[str] = Field(default_factory=list)
    orchestrator: bool = False


class BotUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=MAX_NAME)
    role: Optional[str] = Field(default=None, max_length=MAX_SHORT)
    description: Optional[str] = Field(default=None, max_length=MAX_SHORT)
    avatar: Optional[str] = Field(default=None, max_length=16)
    model: Optional[str] = Field(default=None, max_length=200)
    accent_color: Optional[str] = Field(default=None, max_length=32)
    system_prompt: Optional[str] = Field(default=None, max_length=MAX_PROMPT)
    tools: Optional[List[str]] = None
    pinned: Optional[bool] = None
    archived: Optional[bool] = None
    tool_settings: Optional[Dict[str, Any]] = None
    auto_approval: Optional[Literal["inherit", "off", "shadow", "on"]] = None
    # A number from 0.01 to 0.99, or "inherit" to follow App Settings again.
    gate_threshold: Optional[Any] = None
    trusted_tools: Optional[List[str]] = None
    orchestrator: Optional[bool] = None
    memory: Optional[str] = Field(default=None, max_length=MEMORY_MAX_CHARS)
    todo: Optional[List[Dict[str, Any]]] = Field(default=None, max_length=TODO_MAX_ITEMS)


def _find(bots: List[Dict[str, Any]], bot_id: str) -> int:
    for index, bot in enumerate(bots):
        if bot.get("id") == bot_id:
            return index
    raise HTTPException(status_code=404, detail="Bot not found")


@router.get("", response_model=List[Bot])
async def get_bots():
    # Every account has an orchestrator; the first listing creates it.
    ensure_orchestrator(current_storage())
    return current_storage().get_bots()


@router.get("/{bot_id}", response_model=Bot)
async def get_bot(bot_id: str):
    """One bot as it is now: the client refreshes notes and to-dos with this after a turn."""
    bots = current_storage().get_bots()
    return bots[_find(bots, bot_id)]


@router.post("", response_model=Bot)
async def create_bot(bot_data: BotInput):
    name = bot_data.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="A bot needs a name.")
    model = (bot_data.model or "").strip() or llm_service.current_llm_config().default_model
    bot = {
        "id": f"bot-{uuid.uuid4().hex[:6]}",
        "name": name,
        "role": bot_data.role.strip() or "AI Assistant",
        "description": bot_data.description.strip() or f"Custom agent running {model}.",
        "avatar": bot_data.avatar.strip() or "🤖",
        "model": model,
        "accent_color": bot_data.accent_color.strip() or "#3b82f6",
        "system_prompt": bot_data.system_prompt.strip() or f"You are {name}, a helpful AI assistant.",
        "tools": bot_data.tools,
        "tool_settings": {},
        "auto_approval": "inherit",
        "gate_threshold": None,
        "trusted_tools": clean_trusted_tools(bot_data.trusted_tools),
        "orchestrator": bool(bot_data.orchestrator),
        "pinned": False,
        "archived": False,
        "unread_count": 0,
        "created_at": datetime.now().isoformat(),
    }
    bots = current_storage().get_bots()
    bots.append(bot)
    current_storage().save_bots(bots)
    return bot


@router.put("/{bot_id}", response_model=Bot)
async def update_bot(bot_id: str, updates: BotUpdate):
    bots = current_storage().get_bots()
    index = _find(bots, bot_id)
    changes = updates.model_dump(exclude_none=True)
    if "name" in changes:
        changes["name"] = changes["name"].strip()
        if not changes["name"]:
            raise HTTPException(status_code=422, detail="A bot needs a name.")
    if "model" in changes and not changes["model"].strip():
        changes.pop("model")
    if "tool_settings" in changes:
        changes["tool_settings"] = clean_tool_settings(changes["tool_settings"])
    if "trusted_tools" in changes:
        changes["trusted_tools"] = clean_trusted_tools(changes["trusted_tools"])
    if "gate_threshold" in changes:
        raw = changes["gate_threshold"]
        if raw in ("inherit", "", None):
            changes["gate_threshold"] = None
        else:
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="gate_threshold must be a number from 0.01 to 0.99, or \"inherit\".") from exc
            if not 0.01 <= value <= 0.99:
                raise HTTPException(status_code=422, detail="gate_threshold must be between 0.01 and 0.99.")
            changes["gate_threshold"] = round(value, 2)
    memory_note = None
    if "memory" in changes:
        try:
            memory = clean_memory(changes.pop("memory"))
        except MemoryError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if memory != str(bots[index].get("memory") or ""):
            bots[index]["memory"] = memory
            bots[index]["memory_updated_at"] = datetime.now().isoformat()
            # Recorded in the thread so the model sees the edit where it
            # happened, without the system prompt (and the cache) changing.
            memory_note = memory_note_message(bot_id, memory, message_id=f"msg-{uuid.uuid4().hex[:6]}")
    todo_note = None
    if "todo" in changes:
        try:
            todo = clean_todo(changes.pop("todo"))
        except TodoError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if todo != list(bots[index].get("todo") or []):
            bots[index]["todo"] = todo
            bots[index]["todo_updated_at"] = datetime.now().isoformat()
            todo_note = todo_note_message(bot_id, todo, message_id=f"msg-{uuid.uuid4().hex[:6]}")
    archived = changes.pop("archived", None)
    bots[index].update({key: value for key, value in changes.items() if key in EDITABLE_FIELDS})
    if archived is not None:
        bots[index]["archived"] = archived
        bots[index]["archived_at"] = datetime.now().isoformat() if archived else None
        if archived:
            # An archived bot should not keep working in the background.
            turn_manager.cancel(bot_id)
    current_storage().save_bots(bots)
    if memory_note is not None:
        current_storage().add_message(memory_note)
    if todo_note is not None:
        current_storage().add_message(todo_note)
    return bots[index]


@router.delete("/{bot_id}")
async def delete_bot(bot_id: str):
    """Remove a bot together with its transcript, running turn, and computer."""
    bots = current_storage().get_bots()
    index = _find(bots, bot_id)
    turn_manager.cancel(bot_id)
    try:
        await computer_provider.stop(computer_id_for_bot(bot_id))
    except ComputerProviderError:
        pass  # No computer, or none running: nothing to stop.
    except Exception as exc:  # The deletion must not fail because a container misbehaves.
        logger.warning("Could not stop the computer for %s during deletion: %s", bot_id, exc)
    deleted_messages = current_storage().delete_messages(bot_id)
    current_storage().delete_bot_secrets(bot_id)
    current_storage().delete_bot_schedules(bot_id)
    bots.pop(index)
    current_storage().save_bots(bots)
    return {"status": "ok", "deleted_id": bot_id, "deleted_messages": deleted_messages}
