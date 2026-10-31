"""A bot's automatic memory, for its owner: list and search, correct, delete."""

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services import auto_memory
from app.services.identity import acting_for_owner
from app.services.storage_service import current_storage

router = APIRouter(prefix="/api/v1/bots", tags=["memories"])


class MemoryEdit(BaseModel):
    subject: Optional[str] = Field(default=None, max_length=auto_memory.SUBJECT_MAX)
    text: Optional[str] = Field(default=None, max_length=auto_memory.TEXT_MAX)


def _own_bot(bot_id: str) -> None:
    # A teammate on a shared bot does not see what it remembers of its owner's conversations.
    if not acting_for_owner() or not any(b.get("id") == bot_id for b in current_storage().get_bots()):
        raise HTTPException(status_code=404, detail="No such bot.")


def _guard(action):
    try:
        return action()
    except auto_memory.MemoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{bot_id}/memories")
async def list_memories(bot_id: str, q: str = Query("", max_length=500), history: bool = False):
    """The bot's memories, newest first: the current ones, or with history the replaced and forgotten ones too."""
    _own_bot(bot_id)
    memories = auto_memory.list_memories(bot_id, include_history=history, query=q)
    return {
        "memories": memories,
        "enabled": auto_memory.enabled_for(bot_id),
        "current": len(auto_memory.list_memories(bot_id)),
    }


@router.put("/{bot_id}/memories/{memory_id}")
async def edit_memory(bot_id: str, memory_id: str, body: MemoryEdit) -> Any:
    _own_bot(bot_id)
    return {"memory": _guard(lambda: auto_memory.edit_memory(bot_id, memory_id, subject=body.subject, text=body.text))}


@router.delete("/{bot_id}/memories/{memory_id}")
async def delete_memory(bot_id: str, memory_id: str):
    _own_bot(bot_id)
    _guard(lambda: auto_memory.delete_memory(bot_id, memory_id))
    return {"status": "ok"}
