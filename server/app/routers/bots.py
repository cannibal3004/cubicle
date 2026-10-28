"""Bot personas: create, edit, archive, and delete."""

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.schemas.contracts import Bot
from app.services import llm_service
from app.services.command_rules import clean_rules
from app.services.search import schedule_notes_index
from app.services.bot_memory import MEMORY_MAX_CHARS, MemoryError, clean_memory, memory_note_message
from app.services.bot_todo import TODO_MAX_ITEMS, TodoError, clean_todo, get_todo, todo_note_message
from app.services import conversations
from app.services.computer_provider import ComputerProviderError, computer_id_for_bot, computer_provider
from app.services.orchestrator import ensure_orchestrator
from app.services import procedures as procedure_service
from app.services.storage_service import current_storage
from app.services.turn_manager import turn_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/bots", tags=["bots"])

MAX_NAME = 80
MAX_SHORT = 200
MAX_PROMPT = 20_000
# Only these fields can be changed from the client; ids, counters, and
# timestamps are the server's.
EDITABLE_FIELDS = {"name", "role", "description", "avatar", "model", "accent_color", "system_prompt", "tools", "pinned", "tool_settings", "auto_approval", "gate_threshold", "trusted_tools", "allowed_commands", "procedures", "suggestions"}  # memory is handled separately
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


def clean_allowed_commands(value: Any) -> List[str]:
    try:
        return clean_rules(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
            # Only switch-offs need storing, since enabled is the default; a
            # connected app can also be switched on, over an app that is off
            # by default.
            if flag is False or section == "toolkits":
                kept[key] = flag
        total += len(kept)
        if kept:
            cleaned[section] = kept
    if total > MAX_TOOL_SETTINGS_ENTRIES:
        raise HTTPException(status_code=422, detail="tool_settings has too many entries.")
    return cleaned


MAX_SUGGESTIONS = 6
MAX_SUGGESTION_CHARS = 300


def clean_suggestions(value: Any) -> List[str]:
    """Starter prompts: trimmed, non-empty, distinct, at most six of 300 characters."""
    if not isinstance(value, list):
        raise HTTPException(status_code=422, detail="suggestions must be a list of strings.")
    kept: List[str] = []
    for item in value:
        if not isinstance(item, str):
            raise HTTPException(status_code=422, detail="suggestions must be a list of strings.")
        text = " ".join(item.split())
        if len(text) > MAX_SUGGESTION_CHARS:
            raise HTTPException(status_code=422, detail=f"A suggestion can be at most {MAX_SUGGESTION_CHARS} characters.")
        if text and text not in kept:
            kept.append(text)
    if len(kept) > MAX_SUGGESTIONS:
        raise HTTPException(status_code=422, detail=f"A bot can have at most {MAX_SUGGESTIONS} suggestions.")
    return kept


DRAFT_INSTRUCTION = (
    "Write {count} messages a new user could send this assistant as their first message, to get something useful "
    "done or to see what it can do. Each is one sentence under 90 characters, written the way the user would type "
    "it, and specific to this assistant's job: name the real systems, places and tasks mentioned above. Reply with "
    "only a JSON array of strings."
)


def _draft_brief(bot: Dict[str, Any]) -> str:
    parts = [f"Name: {bot.get('name') or ''}", f"Role: {bot.get('role') or ''}"]
    if bot.get("description"):
        parts.append(f"Description: {bot['description']}")
    if bot.get("system_prompt"):
        parts.append("Its instructions:\n" + str(bot["system_prompt"])[:4000])
    if bot.get("memory"):
        parts.append("Its notes:\n" + str(bot["memory"])[:4000])
    titles = [str(p.get("title") or "") for p in (bot.get("procedures") or []) if isinstance(p, dict) and p.get("title")]
    if titles:
        parts.append("Things it has been shown how to do:\n" + "\n".join(f"- {t}" for t in titles[:20]))
    return "Here is an AI assistant someone set up.\n\n" + "\n\n".join(parts)


def parse_drafted(text: str, count: int) -> List[str]:
    """The prompts in a model reply: a JSON array when there is one, else one per line."""
    match = re.search(r"\[.*\]", text or "", re.S)
    items: List[Any] = []
    if match:
        try:
            parsed = json.loads(match.group(0))
            items = parsed if isinstance(parsed, list) else []
        except ValueError:
            items = []
    if not items:
        items = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip().strip('"') for line in (text or "").splitlines()]
    kept: List[str] = []
    for item in items:
        line = " ".join(str(item).split()) if isinstance(item, (str, int, float)) else ""
        if line and len(line) <= MAX_SUGGESTION_CHARS and line not in kept:
            kept.append(line)
    return kept[:count]


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
    allowed_commands: List[str] = Field(default_factory=list)
    orchestrator: bool = False
    suggestions: List[str] = Field(default_factory=list)


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
    allowed_commands: Optional[List[str]] = None
    procedures: Optional[List[Dict[str, Any]]] = None
    orchestrator: Optional[bool] = None
    memory: Optional[str] = Field(default=None, max_length=MEMORY_MAX_CHARS)
    todo: Optional[List[Dict[str, Any]]] = Field(default=None, max_length=TODO_MAX_ITEMS)
    suggestions: Optional[List[str]] = None


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
        "allowed_commands": clean_allowed_commands(bot_data.allowed_commands),
        "orchestrator": bool(bot_data.orchestrator),
        "suggestions": clean_suggestions(bot_data.suggestions),
        "pinned": False,
        "archived": False,
        "unread_count": 0,
        "created_at": datetime.now().isoformat(),
    }
    bots = current_storage().get_bots()
    bots.append(bot)
    current_storage().save_bots(bots)
    return bot


def _announce_notes(bot_id: str, memory: str) -> None:
    """The user's edit of the notes, shown in each of the bot's conversations that has begun, and in
    its first one (which, unlike the others, does not start with the notes)."""
    for item in conversations.list_for_bot(bot_id):
        if item["archived"] or (not item["messages"] and item["id"] != bot_id):
            continue
        current_storage().add_message(memory_note_message(item["id"], memory, message_id=f"msg-{uuid.uuid4().hex[:6]}", bot_id=bot_id))
        conversations.notes_seen(item["id"])


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
    if "allowed_commands" in changes:
        changes["allowed_commands"] = clean_allowed_commands(changes["allowed_commands"])
    if "suggestions" in changes:
        changes["suggestions"] = clean_suggestions(changes["suggestions"])
    procedures_before = None
    if "procedures" in changes:
        try:
            changes["procedures"] = procedure_service.clean_procedures(changes["procedures"])
        except procedure_service.ProcedureError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        procedures_before = [p for p in (bots[index].get("procedures") or []) if isinstance(p, dict)]
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
            schedule_notes_index(bot_id, str(bots[index].get("name") or bot_id), memory)
            # Recorded in the bot's conversations so the model sees the edit
            # where it happened, without the system prompt (and the cache) changing.
            memory_note = memory
    todo_note = None
    if "todo" in changes:
        try:
            todo = clean_todo(changes.pop("todo"))
        except TodoError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # The list given here is the bot's first conversation's (each conversation has its own).
        if todo != get_todo(bot_id):
            conversations.update(bot_id, {"todo": todo, "todo_updated_at": datetime.now().isoformat()})
            todo_note = todo_note_message(bot_id, todo, message_id=f"msg-{uuid.uuid4().hex[:6]}")
    archived = changes.pop("archived", None)
    bots[index].update({key: value for key, value in changes.items() if key in EDITABLE_FIELDS})
    if archived is not None:
        bots[index]["archived"] = archived
        bots[index]["archived_at"] = datetime.now().isoformat() if archived else None
        if archived:
            # An archived bot should not keep working in the background.
            turn_manager.cancel_bot(bot_id)
    current_storage().save_bots(bots)
    if {"trusted_tools", "allowed_commands", "auto_approval", "gate_threshold"} & set(changes):
        # A card waiting in a running turn may be covered by the new settings.
        from app.routers.chat import schedule_recheck

        schedule_recheck(bot_id)
    if procedures_before is not None:
        procedure_service.sync_index(bot_id, procedures_before, bots[index].get("procedures") or [])
    if memory_note is not None:
        _announce_notes(bot_id, memory_note)
    if todo_note is not None:
        current_storage().add_message(todo_note)
    return bots[index]


@router.post("/{bot_id}/suggestions/draft")
async def draft_suggestions(bot_id: str):
    """Starter prompts drafted from the bot's prompt, notes and procedures, for the user to edit (not saved)."""
    bots = current_storage().get_bots()
    bot = bots[_find(bots, bot_id)]
    config = llm_service.current_llm_config()
    model = bot.get("model") or config.default_model
    count = 4
    messages = [{"role": "user", "content": f"{_draft_brief(bot)}\n\n{DRAFT_INSTRUCTION.format(count=count)}"}]
    pieces: List[str] = []
    try:
        async for event in llm_service.stream_chat_completion(model=model, messages=messages, system_prompt="", config=config, tools=None, one_shot=True):
            if event.get("type") == "content.delta":
                pieces.append(event.get("delta") or "")
            elif event.get("type") == "turn.completed" and not event.get("ok", True):
                raise RuntimeError(event.get("error") or "the request failed")
    except Exception as exc:
        logger.warning("Drafting suggestions for %s failed: %s", bot_id, exc)
        raise HTTPException(status_code=502, detail=f"The model could not draft suggestions: {exc}") from exc
    drafted = parse_drafted("".join(pieces), count)
    if not drafted:
        raise HTTPException(status_code=502, detail="The model's reply had no suggestions in it. Try again.")
    return {"suggestions": drafted}


@router.delete("/{bot_id}")
async def delete_bot(bot_id: str):
    """Remove a bot together with its transcript, running turn, and computer."""
    bots = current_storage().get_bots()
    index = _find(bots, bot_id)
    turn_manager.cancel_bot(bot_id)
    try:
        await computer_provider.stop(computer_id_for_bot(bot_id))
    except ComputerProviderError:
        pass  # No computer, or none running: nothing to stop.
    except Exception as exc:  # The deletion must not fail because a container misbehaves.
        logger.warning("Could not stop the computer for %s during deletion: %s", bot_id, exc)
    deleted_messages = sum(conversations.delete(c["id"]) for c in conversations.list_for_bot(bot_id))
    from app.services import projects

    for project in projects.list_for_bot(bot_id):
        projects.delete(project["id"])
    current_storage().delete_bot_secrets(bot_id)
    current_storage().delete_bot_schedules(bot_id)
    bots.pop(index)
    current_storage().save_bots(bots)
    return {"status": "ok", "deleted_id": bot_id, "deleted_messages": deleted_messages}
