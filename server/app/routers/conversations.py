"""A bot's conversations: list, start, rename, archive, delete, and each one's to-do list."""

import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import conversations, projects, thinking
from app.services.bot_todo import TODO_MAX_ITEMS, TodoError, clean_todo, get_todo, todo_note_message
from app.services.conversations import ConversationError
from app.services.storage_service import current_storage
from app.services.turn_manager import turn_manager

router = APIRouter(prefix="/api/v1", tags=["conversations"])


class ConversationInput(BaseModel):
    title: str = Field(default="", max_length=conversations.TITLE_MAX_CHARS * 2)
    project_id: Optional[str] = Field(default=None, max_length=40)


class ConversationUpdate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=conversations.TITLE_MAX_CHARS * 2)
    archived: Optional[bool] = None
    todo: Optional[List[Dict[str, Any]]] = Field(default=None, max_length=TODO_MAX_ITEMS)
    # Move it into a project; "" takes it out of its project.
    project_id: Optional[str] = Field(default=None, max_length=40)
    # Its own work notes (used while it is in no project).
    work_notes: Optional[str] = Field(default=None, max_length=projects.WORK_NOTES_MAX_CHARS)
    # How hard the model thinks here: "auto", an effort level, or "" to follow the bot.
    thinking: Optional[str] = Field(default=None, max_length=20)


def _record(conversation_id: str) -> Dict[str, Any]:
    if conversations.bot_id_of(conversation_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    record = conversations.get(conversation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return record


def _summary(record: Dict[str, Any]) -> Dict[str, Any]:
    return conversations.summary(record, current_storage().thread_activity().get(record["id"]))


def _busy(conversation_id: str) -> bool:
    current = turn_manager.current(conversation_id)
    return current is not None and not current.finished


@router.get("/bots/{bot_id}/conversations")
async def list_conversations(bot_id: str):
    try:
        return conversations.list_for_bot(bot_id)
    except ConversationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/bots/{bot_id}/conversations")
async def start_conversation(bot_id: str, body: Optional[ConversationInput] = None):
    """A new conversation with the bot: empty, apart from the bot's notes, which every conversation shares."""
    try:
        record = conversations.create(bot_id, body.title if body else "", project_id=(body.project_id or None) if body else None)
    except ConversationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _summary(record)


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    return _summary(_record(conversation_id))


@router.patch("/conversations/{conversation_id}")
async def update_conversation(conversation_id: str, body: ConversationUpdate):
    record = _record(conversation_id)
    changes: Dict[str, Any] = {}
    if body.title is not None:
        changes["title"] = conversations.clean_title(body.title)
        # The user's title is never replaced; clearing it lets a model name the conversation again.
        changes["title_source"] = "user" if changes["title"] else None
        changes["title_tries"] = 0
    if body.thinking is not None:
        try:
            changes["thinking"] = thinking.clean_setting(body.thinking)
        except thinking.ThinkingError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.archived is not None:
        if body.archived and _busy(conversation_id):
            turn_manager.cancel(conversation_id)  # an archived conversation does not keep working
        changes["archived"] = body.archived
    note = None
    if body.todo is not None:
        try:
            todo = clean_todo(body.todo)
        except TodoError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if todo != get_todo(conversation_id):
            changes["todo"] = todo
            # Recorded in the conversation so the bot sees the edit where it happened.
            note = todo_note_message(conversation_id, todo, message_id=f"msg-{uuid.uuid4().hex[:6]}", bot_id=record["bot_id"])
    work_note = None
    if body.work_notes is not None:
        try:
            notes = projects.clean_work_notes(body.work_notes)
        except projects.ProjectError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if notes != str(record.get("work_notes") or ""):
            changes["work_notes"] = notes
            # Shown to the bot where it happened, like an edit of its notes.
            work_note = projects.work_note_message(conversation_id, notes, bot_id=record["bot_id"], by="user")
    if changes:
        record = conversations.update(conversation_id, changes)
    if work_note is not None:
        current_storage().add_message(work_note)
    if body.project_id is not None:
        try:
            record = projects.join(conversation_id, body.project_id or None)
        except projects.ProjectError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.title is not None and not record.get("title"):
        from app.services import conversation_titles

        conversation_titles.schedule(conversation_id)
    if note is not None:
        current_storage().add_message(note)
    return _summary(record)


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    """Delete a conversation and its messages. A bot's first conversation is emptied instead."""
    _record(conversation_id)
    if _busy(conversation_id):
        raise HTTPException(status_code=409, detail="A reply is still in progress; stop it first.")
    deleted = conversations.delete(conversation_id)
    return {"status": "ok", "deleted_id": conversation_id, "deleted_messages": deleted}
