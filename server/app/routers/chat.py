import asyncio
import contextlib
import dataclasses
import json
import time
import logging
import uuid
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from typing import Any, Dict, List, Optional, Union

from app.config import settings
from app.schemas.contracts import TurnRequest, Message
from app.services.command_rules import describe_for_card, script_allowed
from app.services.identity import acting_for_owner, get_current_actor, get_current_owner, owner_context
from app.services.storage_service import current_storage
from app.services import llm_service
from app.services.action_gateway import (
    ActionGatewayError,
    ActionPolicyError,
    action_gateway,
)
from app.services.approval_gate import approval_gate
from app.services.commands import CommandError, parse_action_command
from app.services.composio_service import composio_service
from app.services.connector_tools import connector_tool_specs, shared_connector_specs
from app.services import computer_turns, conversation_titles, conversations, projects, sharing, thinking  # projects: registers its actions
from app.services.context_builder import build_history_messages, cap_message_results, latest_compaction, live_messages, safe_arguments, trim_result_text
from app.services.context_compactor import at_hard_limit, compact_thread, last_prompt_tokens, over_budget
from app.services import bot_memory as _bot_memory  # noqa: F401 - registers memory.read/write
from app.services.bot_todo import get_todo
from app.services.mcp_tools import mcp_tool_specs
from app.services.mcp_tools import mcp_tool_specs
from app.services.mcp_tools import mcp_tool_specs
from app.services.secrets_service import names_for, redactor_for
from app.services import attachments as attachment_service
from app.services import recall, retrieval
from app.services import demonstrations, procedures
from app.services import rewind as rewind_service
from app.services import tool_names
from app.services.repetition import RepetitionGuard, TAIL_CHARS as _LOOP_TAIL
from app.services import stray_calls
from app.services import tool_selection
from app.services.tool_selection import select_tools_kept
from app.services import orchestrator as _orchestrator  # noqa: F401 - registers bots.* actions
from app.services import file_share as _file_share  # noqa: F401 - registers files.share
from app.services.file_share import FileShareError, resolve_shared_file
from app.services.llm_tools import (
    ToolCallError,
    available_tools,
    build_invocation,
    trust_tools,
    describe_tools,
    filter_tools,
    openai_tool_definitions,
    parse_arguments,
    without_duplicate_routes,
)
from app.services.openai_compatible_service import openai_compatible_service
from app.services.turn_manager import Turn, TurnBusyError, turn_manager

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])

# Tool results are fed back to the model as text. Keep them bounded so one
# large file does not blow the context window.
MAX_TOOL_RESULT_CHARS = 20_000

# Why a turn's tools were withdrawn, as told to the model with its last call,
# and the note shown when even that call produced no text.
_STOP_PROMPTS = {
    "rounds": "The tool budget for this turn is used up.",
    "context": "The context window is nearly full and the older history has already been compacted as far as it can be.",
    "stalled": "Your last tool calls repeated earlier calls of this turn and returned the same results, so they are not making progress.",
}
# Shown in the chat when the server stopped the model at its output limit.
_CUT_NOTE = "[The reply was cut off at the model's output limit.]"
# Told to the model with a tool call its cut output left unreadable.
_CUT_HINT = (
    " Your output was cut off at the completion token limit before this call was complete. Make the call much shorter: "
    "write long scripts or data to a file with write_file (in pieces if needed) and run the file, instead of "
    "putting them in one command."
)
_STOP_NOTES = {
    "rounds": "[Stopped after {rounds} tool rounds without a final answer.]",
    "context": "[Stopped: the context window is full and could not be compacted further.]",
    "stalled": "[Stopped: the last tool rounds only repeated earlier calls with the same results.]",
    "cancelled": "[Stopped by you.]",
}


class StartTurnRequest(BaseModel):
    model: Optional[str] = None


def _compact_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Drop binary payloads (screenshots) and trim long text for SSE and the model."""
    compact: Dict[str, Any] = {}
    for key, value in result.items():
        if key == "data" and isinstance(value, str) and len(value) > 2000:
            compact[key] = f"<{len(value)} bytes of base64 image omitted>"
        elif isinstance(value, str) and len(value) > MAX_TOOL_RESULT_CHARS:
            compact[key] = value[:MAX_TOOL_RESULT_CHARS] + f"\n...[truncated {len(value) - MAX_TOOL_RESULT_CHARS} characters]"
        else:
            compact[key] = value
    return compact


def _tool_result_text(payload: Dict[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + "...[truncated]"
    return text


def _screenshot_message(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Turn a screenshot result into an image message the model can look at."""
    data = result.get("data")
    if not isinstance(data, str) or not data:
        return None
    image_format = str(result.get("format") or "jpeg").lower()
    frame = result.get("frame_id") or "latest"
    url = data if data.startswith("data:") else f"data:image/{image_format};base64,{data}"
    size = ""
    if result.get("width") and result.get("height"):
        size = f" {result['width']}x{result['height']}"
        if result.get("scaled") and result.get("screen_width"):
            size += f" (the screen is {result['screen_width']}x{result['screen_height']}, shown scaled; send coordinates in this image's pixels)"
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": f"[Screenshot from computer_screenshot, frame {frame}{size}. This image is the tool result, not a new user message.]"},
            {"type": "image_url", "image_url": {"url": url}},
        ],
    }


def _model_accepts_images(model: str, llm_config) -> bool:
    mode = (settings.LLM_SCREENSHOTS_TO_MODEL or "auto").lower()
    if mode == "never":
        return False
    if mode == "always":
        return True
    info = openai_compatible_service.cached_model_info(llm_config, model)
    return not (info is not None and info.supports_vision is False)


def _ensure_thread(thread_id: str) -> str:
    """A conversation the caller may open, of their own bot or one shared with them; returns its bot's id.

    Anything else, someone else's conversation included, does not exist for them.
    """
    access = sharing.enter_thread(thread_id)
    if access is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    return access.bot_id


@router.get("/history/{thread_id}", response_model=List[Message])
async def get_history(thread_id: str):
    _ensure_thread(thread_id)
    # Attachment text is for the model; the chat gets the card fields.
    messages = []
    for message in current_storage().get_messages(thread_id=thread_id):
        context = ((message.get("raw_payload") or {}).get("turn_context")) if message.get("sender") == "user" else None
        if isinstance(context, dict) and "text" in context:
            # The looked-up text is for the model; the chat gets what was found, by kind.
            message = {**message, "raw_payload": {**message["raw_payload"], "turn_context": {k: v for k, v in context.items() if k != "text"}}}
        if message.get("attachments"):
            message = {**message, "attachments": attachment_service.without_text(message["attachments"])}
        messages.append(message)
    return messages


@router.post("/compact/{thread_id}")
async def compact_now(thread_id: str):
    """Summarise the older part of a thread now (the /compact command)."""
    current = turn_manager.current(thread_id)
    if current is not None and not current.finished:
        raise HTTPException(status_code=409, detail="A reply is still in progress; wait for it or use /stop first.")
    setup = await _turn_setup(thread_id, None)
    try:
        stored = await compact_thread(
            thread_id, reason="manual", model=setup["selected_model"], system_prompt=setup["compaction_prompt"],
            config=setup["llm_config"], storage=current_storage(), replay_tools=setup["llm_config"].provider != "muapi",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Compaction failed: {exc}") from exc
    if stored is None:
        return {"status": "nothing_to_compact", "keep_recent_tokens": settings.CONTEXT_KEEP_RECENT_TOKENS}
    return {"status": "ok", "compaction": stored}


@router.delete("/history/{thread_id}")
async def clear_history(thread_id: str):
    """Delete a thread's messages (the /clear command). Refused mid-reply."""
    current = turn_manager.current(thread_id)
    if current is not None and not current.finished:
        raise HTTPException(status_code=409, detail="A reply is still in progress; stop it first.")
    deleted = current_storage().delete_messages(thread_id)
    current_storage().clear_rewinds(thread_id)
    _store_tool_selection(thread_id, None)  # a fresh conversation chooses its tools afresh
    _store_conversation_field(thread_id, "tool_list", None)
    return {"status": "ok", "deleted": deleted}


# ---- undo and redo (services/rewind.py) ---------------------------------------------


class RewindRequest(BaseModel):
    message_id: Optional[str] = None


async def _stop_running_turn(thread_id: str, wait_seconds: float = 15.0) -> None:
    """Cancel the thread's reply in progress and wait until it has stored what it had."""
    current = turn_manager.current(thread_id)
    if current is None or current.finished:
        return
    turn_manager.cancel(thread_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_seconds
    while not current.finished and loop.time() < deadline:
        await asyncio.sleep(0.1)
    if not current.finished:
        raise HTTPException(status_code=409, detail="The reply did not stop in time; try again in a moment.")


def _for_client(message: Dict[str, Any]) -> Dict[str, Any]:
    if message.get("attachments"):
        return {**message, "attachments": attachment_service.without_text(message["attachments"])}
    return message


@router.get("/rewind/{thread_id}")
async def rewind_preview(thread_id: str, message_id: Optional[str] = None):
    """What rewinding to just before a message removes, and which of the bot's actions stay done."""
    _ensure_thread(thread_id)
    try:
        return rewind_service.preview(thread_id, message_id)
    except rewind_service.RewindError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/rewind/{thread_id}")
async def rewind_thread(thread_id: str, body: Optional[RewindRequest] = None):
    """Edit or /undo: remove a message of the user's and everything after it (stopping a reply in progress)."""
    _ensure_thread(thread_id)
    await _stop_running_turn(thread_id)
    try:
        result = rewind_service.rewind(thread_id, body.message_id if body else None)
    except rewind_service.RewindError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "ok", **result, "message": _for_client(result["message"])}


@router.get("/redo/{thread_id}")
async def redo_status(thread_id: str):
    _ensure_thread(thread_id)
    return rewind_service.redo_state(thread_id)


@router.post("/redo/{thread_id}")
async def redo_thread(thread_id: str):
    """/redo: put back what the last undo removed, while nothing new has been added."""
    _ensure_thread(thread_id)
    current = turn_manager.current(thread_id)
    if current is not None and not current.finished:
        raise HTTPException(status_code=409, detail="A reply is in progress; stop it first.")
    try:
        result = rewind_service.redo(thread_id)
    except rewind_service.RewindError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "ok", **result}


@router.post("/send")
async def send_message(req: TurnRequest):
    # The conversation decides the bot, whatever the client sent as bot_id.
    bot_id = _ensure_thread(req.thread_id)
    first_message = not any(m.get("sender") == "user" for m in current_storage().get_messages(thread_id=req.thread_id))
    # Store user message
    user_msg = {
        "id": f"msg-{uuid.uuid4().hex[:6]}",
        "thread_id": req.thread_id,
        "bot_id": bot_id,
        "sender": "user",
        "text": req.user_text,
        "image_url": req.image_url,
        "created_at": datetime.now().isoformat(),
        "model": req.model or llm_service.current_llm_config().default_model,
        "item_type": "user_text",
        # Who wrote it: on a shared bot, the teammate rather than the owner.
        "author_id": get_current_actor(),
    }
    if req.attachments:
        if len(req.attachments) > attachment_service.MAX_PER_MESSAGE:
            raise HTTPException(status_code=400, detail=f"At most {attachment_service.MAX_PER_MESSAGE} files can be attached to one message.")
        attached = []
        for ref in req.attachments:
            try:
                shared = resolve_shared_file(ref.source, ref.path, bot_id)
            except FileShareError as exc:
                raise HTTPException(status_code=400, detail=f"Attachment {ref.path}: {exc}")
            attached.append(await attachment_service.attach(shared, bot_id))
        user_msg["attachments"] = attached
    if req.recording_id:
        record = demonstrations.load_recording(bot_id, req.recording_id)
        if record is None:
            raise HTTPException(status_code=404, detail="That recording does not exist.")
        if not user_msg["text"].strip():
            user_msg["text"] = "I showed you how to do something on your computer."
        user_msg["raw_payload"] = {"demonstration": demonstrations.summary(record)}

    current_storage().add_message(user_msg)
    recall.schedule_index(req.thread_id, user_msg)
    if first_message:
        # A conversation is named after what it was started with, until the user renames it.
        record = conversations.get(req.thread_id)
        if record is not None and not record.get("title"):
            conversations.update(req.thread_id, {"title": conversations.title_from(user_msg["text"]), "title_source": "first_message"})
    # A new message starts a new branch: what was undone can no longer be redone.
    rewind_service.forget(req.thread_id)
    if user_msg.get("attachments"):
        user_msg = {**user_msg, "attachments": attachment_service.without_text(user_msg["attachments"])}
    return {"status": "ok", "message": user_msg}


async def _perform_action(
    turn: Turn,
    thread_id: str,
    action_call: Any,
    call_name: Optional[str] = None,
    gate_mode: str = "off",
    user_request: str = "",
    gate_threshold: Optional[float] = None,
    trusted: Union[bool, str] = False,
    specs: Optional[List[Any]] = None,
    bot_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one governed action through the gateway, emitting events on the turn.

    Returns an outcome dict describing what happened so the caller can tell
    the model (or the system prompt) about it. `gate_mode` is the bot's
    effective auto-approval mode; `user_request` is the user's latest message,
    which the gate shows the decision model next to the proposed action.
    `trusted` means the user trusts this tool for this bot: no card, no gate.
    """
    bot_id = bot_id or conversations.bot_id_of(thread_id) or thread_id
    try:
        action_request, approval = action_gateway.open(thread_id, bot_id, action_call, **({"trusted": trusted} if trusted else {}))
    except ActionPolicyError as exc:
        turn.emit({
            "type": "tool.failed",
            "tool": getattr(action_call, "name", "unknown"),
            "callName": call_name,
            "requestId": exc.request_id,
            "error": str(exc),
        })
        return {"status": "rejected", "error": f"Action rejected by policy: {exc}", "request_id": exc.request_id}

    action_name = f"{action_request.tool}.{action_request.action}"
    base = {"tool": action_name, "callName": call_name, "requestId": action_request.request_id}
    if trusted and not approval:
        base["trusted"] = True
        base["trustedBy"] = trusted if isinstance(trusted, str) else "trusted"

    if approval:
        turn.emit({
            "type": "request.opened",
            "requestType": "permission",
            "requestId": action_request.request_id,
            "tool": approval["tool"],
            "callName": call_name,
            "summary": approval["summary"],
            "arguments": approval["arguments"],
            "action": action_request.model_dump(),
            # For a terminal command: the rules the card can offer to remember.
            **(describe_for_card(str(getattr(action_call, "arguments", {}).get("command") or "")) if action_name == "computer.terminal_execute" else {}),
        })
        turn.set_pending_approval({
            "request_id": action_request.request_id,
            "tool": approval["tool"],
            "call_name": call_name,
            "summary": approval["summary"],
            "created_at": approval.get("created_at"),
        })

    # The auto-approval gate scores the action while the card is up. In "on"
    # mode a low-risk action is approved here on the user's behalf; otherwise
    # (shadow, a risky score, or any error) the card stays and the user decides.
    card: Optional[Dict[str, Any]] = None
    if approval:
        card = {
            "request": action_request, "call": action_call, "call_name": call_name, "user_request": user_request,
            "gate_mode": gate_mode, "specs": specs or [], "turn": turn, "base": base, "owner": get_current_owner(), "resolved_by": None, "bot_id": bot_id,
        }
        _open_cards.setdefault(thread_id, {})[action_request.request_id] = card

    verdict = None
    if approval and gate_mode in ("shadow", "on"):
        verdict = await approval_gate.evaluate(
            action_request, action_call, mode=gate_mode, user_request=user_request, threshold=gate_threshold
        )
        approval_gate.record(action_request, verdict)
        turn.emit({"type": "gate.scored", **base, "verdict": verdict.to_event()})
        if verdict.auto_approved:
            action_gateway.resolve_approval(action_request, "allow", decided_by=verdict.decided_by)

    try:
        decision = await action_gateway.wait_for_decision(action_request)
    finally:
        turn.set_pending_approval(None)
        if card is not None:
            (_open_cards.get(thread_id) or {}).pop(action_request.request_id, None)
            if not _open_cards.get(thread_id):
                _open_cards.pop(thread_id, None)
    rechecked_by = card.get("resolved_by") if card is not None and decision == "allow" else None
    if card is not None and card.get("verdict") is not None and verdict is None:
        verdict = card["verdict"]  # scored after auto-approval was switched on mid-wait
    if rechecked_by in ("trusted", "allowed_command"):
        # The user's new settings allowed the card while it waited.
        base["trusted"] = True
        base["trustedBy"] = rechecked_by

    if verdict is not None:
        decided_by = verdict.decided_by if verdict.auto_approved and decision == "allow" and rechecked_by in (None, verdict.decided_by) else "user"
        approval_gate.record_final(action_request.request_id, decision, decided_by)
        base["gate"] = verdict.to_event()

    if decision == "deny":
        turn.emit({"type": "tool.denied", **base})
        return {"status": "denied", "error": f"The user denied this action: {action_request.preview}", "request": action_request, "gate": verdict}
    if decision != "allow":
        turn.emit({"type": "tool.expired", **base})
        return {"status": "expired", "error": f"The approval request expired before the user answered: {action_request.preview}", "request": action_request, "gate": verdict}

    turn.emit({"type": "tool.started", **base, "action": action_request.model_dump()})
    # Whatever comes back is scrubbed of the user's secret values before the
    # model, the transcript or the screen sees it: a shell can print them.
    redact = redactor_for(bot_id)

    def waiting(current: Dict[str, Any]) -> None:
        other = conversations.get(current.get("thread_id") or "") or {}
        where = f"\"{other['title']}\"" if other.get("title") else "another conversation"
        turn.emit({"type": "tool.waiting", **base, "reason": f"Waiting for the computer: {where} is running {current.get('action') or 'something'}."})

    try:
        if computer_turns.takes_turns(action_name):
            # The bot's other conversations share this computer: changes to it take turns.
            async with computer_turns.computer_turn(bot_id, thread_id, _computer_label(action_request), on_wait=waiting):
                action_result = await action_gateway.execute(action_request)
        else:
            action_result = await action_gateway.execute(action_request)
    except ActionGatewayError as exc:
        error = redact(str(exc))
        turn.emit({"type": "tool.failed", **base, "error": error})
        return {"status": "failed", "error": f"Action could not execute ({action_name}): {error}", "request": action_request}

    if action_result.status == "completed":
        result = redact.apply(action_result.result or {})
        turn.emit({"type": "tool.completed", **base, "result": _compact_result(result)})
        return {"status": "completed", "result": result, "request": action_request, "gate": verdict,
                **({"trusted_by": rechecked_by} if rechecked_by in ("trusted", "allowed_command") else {})}

    error = redact(action_result.error or "The action failed.")
    turn.emit({"type": "tool.failed", **base, "error": error})
    return {"status": "failed", "error": f"Action failed ({action_name}): {error}", "request": action_request, "gate": verdict}


def _computer_label(action_request: Any) -> str:
    """What an action does to the computer, as the waiting note names it: the command, the file, or the page."""
    arguments = getattr(action_request, "arguments", None) or {}
    for key in ("command", "path", "url"):
        value = str(arguments.get(key) or "").strip()
        if value:
            value = " ".join(value.split())
            return f"`{value[:100]}{'…' if len(value) > 100 else ''}`"
    return getattr(action_request, "action", "") or "something"


async def _context_window_for(llm_config, model: str) -> Optional[int]:
    """The model's context length from the catalog, fetching it once if the cache is cold."""
    if llm_config.provider == "muapi":
        return None
    info = openai_compatible_service.cached_model_info(llm_config, model)
    if info is None:
        try:
            catalog = await llm_service.list_models(llm_config)
        except Exception:  # The catalog is a nicety; a turn must not fail on it.
            return None
        info = next((m for m in catalog.models if m.id == model), None)
    return info.context_length if info is not None else None


logger = logging.getLogger(__name__)

# ---- approval settings that change during a turn ------------------------------------
# A turn used to fix the bot's auto-approval mode, trusted tools and allowed
# commands when it started. Now each call reads them fresh (live_bot), and an
# approval card already waiting is looked at again when they are saved
# (recheck_pending): a card the new settings cover is allowed on the user's
# behalf, the way the auto-approval gate allows one.

# thread id -> request id -> what is needed to judge the card again
_open_cards: Dict[str, Dict[str, Dict[str, Any]]] = {}


def live_bot(bot_id: str, fallback: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The bot record as it is now (settings may have changed since the turn began)."""
    try:
        return next((b for b in current_storage().get_bots() if b.get("id") == bot_id), None) or fallback
    except Exception:
        return fallback


def trusted_for(bot: Optional[Dict[str, Any]], call_name: Optional[str], arguments: Dict[str, Any], specs: List[Any]) -> Union[bool, str]:
    """Whether the bot's current settings let this call run without a card: "trusted", "allowed_command" or False."""
    if not bot or not call_name:
        return False
    if call_name in set(tool_names.current_names(bot.get("trusted_tools") or [])) and any(spec.name == call_name for spec in specs):
        return "trusted"
    return command_trusted(bot, call_name, arguments)


async def recheck_pending(thread_id: str) -> int:
    """Look at the thread's open cards again under the bot's current settings. Returns how many were allowed."""
    owner = get_current_owner()
    entries = [e for e in (_open_cards.get(thread_id) or {}).values() if e.get("owner") == owner and not e.get("resolved_by")]
    if not entries:
        return 0
    bot = live_bot(entries[0].get("bot_id") or conversations.bot_id_of(thread_id) or thread_id)
    if bot is None:
        return 0
    allowed = 0
    for entry in entries:
        arguments = dict(getattr(entry["call"], "arguments", {}) or {})
        by = trusted_for(bot, entry.get("call_name"), arguments, entry.get("specs") or [])
        if not by and approval_gate.mode_for(bot) == "on" and entry.get("gate_mode") != "on":
            # Auto-approval was switched on while the card waited: score it now.
            entry["gate_mode"] = "on"
            try:
                verdict = await approval_gate.evaluate(
                    entry["request"], entry["call"], mode="on", user_request=entry.get("user_request") or "", threshold=approval_gate.threshold_for(bot)
                )
            except Exception as exc:
                logger.warning("Re-scoring an open card failed: %s", exc)
                continue
            approval_gate.record(entry["request"], verdict)
            entry["verdict"] = verdict
            turn = entry.get("turn")
            if turn is not None:
                turn.emit({"type": "gate.scored", **entry["base"], "verdict": verdict.to_event()})
            if verdict.auto_approved:
                by = verdict.decided_by
        if by and not entry.get("resolved_by"):
            entry["resolved_by"] = by
            if action_gateway.resolve_approval(entry["request"], "allow", decided_by=by):
                allowed += 1
    return allowed


async def recheck_all_pending() -> int:
    """After an app-wide change (auto-approval for every bot): every open card of this account."""
    total = 0
    for thread_id in list(_open_cards):
        total += await recheck_pending(thread_id)
    return total


async def recheck_bot(bot_id: str) -> int:
    """After a change to one bot's settings: the open cards in all of its conversations."""
    total = 0
    for thread_id, cards in list(_open_cards.items()):
        if any(card.get("bot_id") == bot_id for card in cards.values()):
            total += await recheck_pending(thread_id)
    return total


def schedule_recheck(bot_id: Optional[str] = None) -> None:
    """Re-check open cards in the background (from a settings save)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(recheck_bot(bot_id) if bot_id else recheck_all_pending())


_LOOP_NUDGE = (
    "[Your last output fell into a loop: it kept repeating \"{sample}\" and was cut off there. Do not repeat it. "
    "Step back, decide the next concrete step, and continue the task, or answer the user if you are done.]"
)
_LOOP_STOP_NOTE = "[Stopped: the model kept repeating itself. Send a message to steer it.]"


def _cut_loop(text: str, sample: str) -> str:
    """The text up to the first time the repeated part appears in its tail (one copy kept)."""
    if not sample:
        return text
    window_start = max(0, len(text) - _LOOP_TAIL)
    flat = text
    at = flat.find(sample, window_start)
    if at < 0:
        # The sample is whitespace-normalised; fall back to the first line of it.
        head = sample.split(" ")[0]
        at = flat.find(head, window_start) if head else -1
        if at < 0:
            return text
        return text[: at + len(head)]
    return text[: at + len(sample)]


_STRAY_NUDGE = (
    "[You wrote a tool call inside your thinking, where calls cannot run, and it could not be read, so it did not run "
    "and there is no result. Make the call again as a real tool call, outside your thinking, and do not assume any result "
    "until it comes back.]"
)
_STRAY_NUDGES_MAX = 2


def _resolve_calls(raw_calls: List[Dict[str, Any]], tool_specs: List[Any], callable_specs: List[Any], written_as_text: Optional[str] = None) -> List[Dict[str, Any]]:
    """The model's calls with former or habitual names resolved to the real tools."""
    offered_names = [spec.name for spec in tool_specs]
    callable_names = [spec.name for spec in callable_specs]
    calls: List[Dict[str, Any]] = []
    for raw_call in raw_calls or []:
        resolved = tool_names.resolve(raw_call.get("name"), offered_names, callable_names)
        call = raw_call if resolved == raw_call.get("name") else {**raw_call, "name": resolved, "called_as": raw_call.get("name")}
        if written_as_text:
            call = {**call, "written_as_text": written_as_text}
        calls.append(call)
    return calls


def _reply_text(pieces: List[str]) -> str:
    """The stored reply: each round's text, separated by a blank line."""
    return "\n\n".join(piece.strip("\n") for piece in pieces if piece.strip())


def parse_arguments_quietly(raw: Any) -> Any:
    """A call's arguments for comparing two calls; unparsable text compares as itself."""
    try:
        return parse_arguments(raw)
    except Exception:
        return str(raw)


def command_trusted(bot: Optional[Dict[str, Any]], call_name: str, arguments: Dict[str, Any]) -> Union[bool, str]:
    """"allowed_command" when the bot's rules cover every command this terminal call runs."""
    if call_name != "computer_terminal_execute" or not bot:
        return False
    rules = [r for r in (bot.get("allowed_commands") or []) if isinstance(r, str)]
    if not rules:
        return False
    allowed, _ = script_allowed(str(arguments.get("command") or ""), rules)
    return "allowed_command" if allowed else False


async def retrieved_context(bot_id: str, query: str) -> str:
    """The passages of this bot's attachments that match the message, as a prompt block; "" when none."""
    try:
        config = retrieval.current_config()
        if not config.enabled:
            return ""
        hits = await retrieval.retrieve(query, kind="attachment", ref_prefix=f"{bot_id}:", config=config, relevant_only=True)
    except Exception as exc:  # never let retrieval fail a turn
        logger.warning("Retrieval failed for %s: %s", bot_id, exc)
        return ""
    # Files are attached in someone's conversation: on a shared bot each person's
    # turns get passages of their own files only (older entries are the owner's).
    actor, owner = get_current_actor(), get_current_owner()
    hits = [hit for hit in hits if (hit.meta.get("user_id") or owner) == actor]
    return retrieval.render_hits(
        hits,
        heading="Passages from the attached files that match this message (retrieved by similarity; the file itself holds the rest):",
    )


def _store_thinking_hint(history: List[Dict[str, Any]], hint: Optional[str]) -> bool:
    """Keep this turn's thinking hint on the latest user message (or drop an old one). True when it changed.

    Only the latest message changes, at the very end of the prompt: earlier messages keep
    the hints they were answered with, so the start of the prompt stays as cached."""
    latest = next((m for m in reversed(history) if m.get("sender") == "user"), None)
    if latest is None:
        return False
    payload = dict(latest.get("raw_payload") or {})
    if payload.get("thinking_hint") == hint or (not hint and "thinking_hint" not in payload):
        return False
    if hint:
        payload["thinking_hint"] = hint
    else:
        payload.pop("thinking_hint", None)
    latest["raw_payload"] = payload
    try:
        current_storage().update_message(latest)
    except Exception as exc:  # this turn still gets it; a later turn would replay without it
        logger.warning("Could not store the thinking hint of %s: %s", latest.get("id"), exc)
    return True


def _fresh_entries(block: str, marker: str, seen: str) -> str:
    """The block without the entries already in `seen` (attached to an earlier message); "" when none is left.

    A block is a heading line, then entries that each begin with `marker` ("--- " for passages and
    procedures, "- " for recalled exchanges, which are one line each).
    """
    if not block or not seen:
        return block
    lines = block.split("\n")
    heading, entries, current = lines[0], [], []
    for line in lines[1:]:
        if line.startswith(marker) and (marker == "- " or line.rstrip().endswith("---")):
            if current:
                entries.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        entries.append("\n".join(current))
    fresh = [entry for entry in entries if entry.strip() not in seen]
    return "\n".join([heading] + fresh) if fresh else ""


def _store_tool_selection(thread_id: str, keep: Optional[Dict[str, Any]]) -> None:
    """Remember the conversation's tool selection for this stretch of it."""
    _store_conversation_field(thread_id, "tool_selection", keep)


def _store_conversation_field(thread_id: str, field: str, value: Any) -> None:
    try:
        if conversations.get(thread_id) is not None:
            conversations.update(thread_id, {field: value})
    except Exception as exc:
        logger.warning("Could not keep the %s of %s: %s", field.replace("_", " "), thread_id, exc)


def _store_added_tools(history: List[Dict[str, Any]], definitions: List[Dict[str, Any]]) -> bool:
    """Describe the tools offered since the stretch began on the latest user message. True when it changed.

    Like the thinking hint, only the latest message changes, at the end of the prompt; a
    turn run again for the same message keeps what that message already describes."""
    latest = next((m for m in reversed(history) if m.get("sender") == "user"), None)
    if latest is None or not definitions:
        return False
    payload = dict(latest.get("raw_payload") or {})
    existing = payload.get("tools_added") or {}
    names = list(existing.get("names") or [])
    fresh = [d for d in definitions if (d.get("function") or {}).get("name") not in names]
    if not fresh:
        return False
    block = tool_selection.render_added(fresh)
    payload["tools_added"] = {
        "names": names + [(d.get("function") or {}).get("name") for d in fresh],
        "text": f"{existing['text']}\n\n{block}" if existing.get("text") else block,
    }
    latest["raw_payload"] = payload
    try:
        current_storage().update_message(latest)
    except Exception as exc:  # this turn still has them; a later turn describes them again
        logger.warning("Could not store the added tools of %s: %s", latest.get("id"), exc)
    return True


async def attach_turn_context(
    turn: Turn, thread_id: str, bot_msg_id: str, history: List[Dict[str, Any]], query: str, bot_id: Optional[str] = None
) -> bool:
    """Look up the context for the latest user message and store it on that message. True when it was added."""
    latest = next((m for m in reversed(history) if m.get("sender") == "user"), None)
    if latest is None or not (query or "").strip():
        return False
    payload = dict(latest.get("raw_payload") or {})
    if "turn_context" in payload:
        return False  # looked up already (this message's turn is running again): keep it as it was
    blocks: List[str] = []
    counts: Dict[str, int] = {}
    # What earlier messages since the last compaction already carry is still in the
    # context: attach only what is new, so the same passages do not pile up.
    seen = "\n".join(
        str(((m.get("raw_payload") or {}).get("turn_context") or {}).get("text") or "")
        for m in live_messages(history) if m.get("sender") == "user" and m is not latest
    )
    # Passages from long attachments that match this message: the inlined part of a file is only its beginning.
    bot_id = bot_id or thread_id
    retrieved = _fresh_entries(await retrieved_context(bot_id, query), "--- ", seen)
    if retrieved:
        counts["passages"] = retrieved.count("--- from ")
        turn.emit({"type": "retrieval.used", "botMsgId": bot_msg_id, "count": counts["passages"]})
        blocks.append(retrieved)
    # Procedures the user showed the bot that match this message, in full.
    known = _fresh_entries(await procedure_context(bot_id, query), "--- ", seen)
    if known:
        counts["procedures"] = known.count("\n--- ")
        turn.emit({"type": "procedure.used", "botMsgId": bot_msg_id, "count": counts["procedures"]})
        blocks.append(known)
    # Entries of the conversation's project log that match this message.
    conversation = conversations.get(thread_id) or {}
    if conversation.get("project_id"):
        logged = _fresh_entries(await projects.log_context(conversation["project_id"], query), "--- ", seen)
        if logged:
            counts["log_entries"] = logged.count("--- from ")
            turn.emit({"type": "project_log.used", "botMsgId": bot_msg_id, "count": counts["log_entries"]})
            blocks.append(logged)
    # Past exchanges of this conversation that the compaction folded away but that match this message.
    recalled = _fresh_entries(await recalled_context(thread_id, query, history), "- ", seen)
    if recalled:
        counts["recalled"] = recalled.count("\n- ")
        turn.emit({"type": "recall.used", "botMsgId": bot_msg_id, "count": counts["recalled"]})
        blocks.append(recalled)
    # Stored even when empty, so running the turn again does not look again.
    payload["turn_context"] = {"text": "\n\n".join(blocks), **counts}
    latest["raw_payload"] = payload
    try:
        current_storage().update_message(latest)
    except Exception as exc:  # the turn still gets the context, just not the stable replay
        logger.warning("Could not store the context of %s: %s", latest.get("id"), exc)
    return bool(blocks)


async def procedure_context(bot_id: str, query: str) -> str:
    """The bot's procedures that match the message, in full; "" when none."""
    try:
        return await procedures.procedure_context(bot_id, query)
    except Exception as exc:  # never let it fail a turn
        logger.warning("Procedure retrieval failed for %s: %s", bot_id, exc)
        return ""


async def recalled_context(bot_id: str, query: str, history: List[Dict[str, Any]]) -> str:
    """Earlier exchanges of this conversation (`bot_id` is the thread id) that match the message and are no longer in the context; "" when none."""
    try:
        config = retrieval.current_config()
        if not config.enabled:
            return ""
        recall.ensure_backfilled(bot_id, history, config=config)
        return await recall.recall_for_turn(bot_id, query, history, config=config)
    except Exception as exc:  # never let recall fail a turn
        logger.warning("Recall failed for %s: %s", bot_id, exc)
        return ""


def describe_user(bot: Optional[Dict[str, Any]] = None) -> str:
    """One line about the person the bot works for, from their profile; empty when unset.

    On a bot shared with them it is always there, saying whose bot it is: the
    bot's notes are mostly about its owner, and must not be taken for them.
    """
    try:
        from app.services.auth_service import auth_service

        user = auth_service.accounts.get_user(get_current_actor())
        owner = None if acting_for_owner() else auth_service.accounts.get_user(get_current_owner())
    except Exception:
        return ""
    if not user:
        return ""
    name = (user.get("display_name") or "").strip()
    email = (user.get("email") or "").strip()
    if not name and not email and not (user.get("about") or "").strip() and owner is None:
        return ""
    parts = []
    if owner is not None:
        owner_name = sharing.person_name(owner)
        who = name or user.get("username")
        parts.append(
            f"You belong to {owner_name}, who shared you with teammates. In this conversation you work for {who} "
            f"(account {user.get('username')}), not {owner_name}: your notes and history may be about {owner_name}, so do not take "
            f"them for {who}. Address them as {who}."
        )
        if bot is not None:
            parts.append(sharing.describe_for_teammate(bot, owner_name, who))
    elif name:
        parts.append(f"The person you work for is {name} (account {user.get('username')}); address them as {name}.")
    else:
        parts.append(f"The person you work for has the account {user.get('username')}.")
    if email:
        parts.append(f"Their email address is {email}; use it when they ask to be copied, notified or contacted, without asking for it again.")
    line = " ".join(parts) + "\n\n"
    about = (user.get("about") or "").strip()
    if about:
        # What they want every bot to know about them; it belongs here, not in a bot's notes.
        line += f"What they want you to know about them:\n{about}\n\n"
    return line


async def _turn_setup(thread_id: str, requested_model: Optional[str]) -> Dict[str, Any]:
    """Everything a turn (or a compaction) needs, captured once.

    The system prompt is built so that it is byte-identical from one turn to
    the next: the provider's prompt cache resumes from the end of the last
    turn only while the prefix does not change, so the date goes at the end
    and at day resolution, and nothing per-turn goes in here.
    """
    bots = current_storage().get_bots()
    bot_id = conversations.bot_id_of(thread_id)
    current_bot = next((b for b in bots if b["id"] == bot_id), None)
    if current_bot is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    conversation = conversations.get(thread_id) or {}
    history = current_storage().get_messages(thread_id=thread_id)

    raw_prompt = current_bot["system_prompt"] if current_bot else "You are a helpful AI assistant."
    today = datetime.now().strftime("%A, %B %d, %Y")
    system_prompt = f"{raw_prompt}\n\n{describe_user(current_bot)}Today is {today}."
    llm_config = llm_service.current_llm_config()
    # How hard the model thinks: the conversation's setting, the bot's, or the app's. For
    # Auto the turn decides per message; until then (and for a compaction) the level the
    # conversation last used, whose prompt cache is the warm one.
    app_effort = llm_config.reasoning_effort
    standing = thinking.wire(thinking.standing_level(current_bot, conversation, app_effort))[0]
    if standing != llm_config.reasoning_effort:
        llm_config = dataclasses.replace(llm_config, reasoning_effort=standing)
    selected_model = (
        requested_model
        or (current_bot.get("model") if current_bot else None)
        or llm_config.default_model
    )

    # Recent bot turns replay their tool calls and results in the provider's
    # native shape, so the model builds on what it already found.
    formatted_history = build_history_messages(history, replay_tools=llm_config.provider != "muapi")
    context_window = await _context_window_for(llm_config, selected_model)

    # Model tool calling is only possible on OpenAI-compatible servers. MUAPI
    # takes a single prompt, so there the slash commands remain the only tools.
    tools_enabled = settings.LLM_TOOLS_ENABLED and llm_config.provider != "muapi"
    tool_specs: List[Any] = []
    # Everything the bot may call this turn, including app tools that tool
    # selection did not list: a call to one of those still runs.
    callable_specs: List[Any] = []
    selection = None
    if tools_enabled:
        # Every app connected through Composio becomes a set of tools. If the
        # catalog cannot be fetched, fall back to the two built-in GitHub tools.
        # A teammate on a shared bot gets the apps its owner shared, through the
        # owner's connection or their own; the bot's own MCP servers are part of the bot.
        owners_apps = acting_for_owner()
        if owners_apps:
            connector_specs = await connector_tool_specs(composio_service)
        else:
            connector_specs = await shared_connector_specs(composio_service, current_bot)
        # A teammate connects only the apps the bot uses with each person's own account.
        connects_own = not owners_apps and sharing.OWN_CONNECTION in sharing.share_settings(current_bot)["apps"].values()
        # MCP servers the user runs join the same family as connected apps.
        connector_specs = connector_specs + mcp_tool_specs(bot_id=bot_id)
        tool_specs = available_tools(
            computer=settings.COMPUTER_PROVIDER == "docker",
            github=owners_apps and bool(composio_service.get_api_key()) and not connector_specs,
            connectors=connector_specs,
            connector_management=(owners_apps or connects_own) and bool(composio_service.get_api_key()),
            orchestrator=bool(current_bot.get("orchestrator")),
        )
        # The bot's own tool choices, over the apps that are off by default.
        tool_specs = filter_tools(
            tool_specs,
            current_bot.get("tool_settings") if current_bot else None,
            current_storage().get_settings().get("disabled_toolkits") or [],
        )
        # A bot whose computer has the shared folder at /shared uses it there.
        tool_specs = without_duplicate_routes(tool_specs)
        # The user's standing approvals for this bot.
        tool_specs = trust_tools(tool_specs, current_bot.get("trusted_tools") if current_bot else None)
        callable_specs = list(tool_specs)
        # Too many app tools for one round: keep the ones that match the message,
        # chosen once per stretch of conversation (until the next compaction) so
        # the tool list, at the very start of the prompt, does not change from
        # message to message; an app named by the user or the bot is added.
        latest_user = next((m for m in reversed(history) if m.get("sender") == "user" and m.get("text")), None)
        latest_bot = next((m for m in reversed(history) if m.get("sender") == "bot" and m.get("text")), None)
        compaction = latest_compaction(history)
        try:
            stored_selection = conversation.get("tool_selection")
            tool_specs, selection, keep = await select_tools_kept(
                tool_specs,
                str(latest_user.get("text") or "") if latest_user else "",
                kept=stored_selection,
                since=str(compaction.get("id")) if compaction else "",
                mention_text=" ".join(str(m.get("text") or "")[-4000:] for m in (latest_user, latest_bot) if m),
                history=history,
            )
            if keep is not None and keep != stored_selection:
                _store_tool_selection(thread_id, keep)
        except Exception as exc:  # never let selection fail a turn
            logger.warning("Tool selection failed for %s: %s", thread_id, exc)
            selection = None
    selection_note = selection.note() if selection else ""
    # The tool list (and the system prompt's account of it) the model is sent; with
    # TOOL_ADD_STYLE=message the one this stretch began with, and what has been offered
    # since goes in this turn's message instead.
    listed_specs, tools_added = tool_specs, []
    if tool_specs and tool_selection.adds_in_message():
        try:
            stored_list = conversation.get("tool_list")
            listed_specs, added_specs, keep_list = tool_selection.hold_list(
                tool_specs, callable_specs, kept=stored_list,
                since=str(compaction.get("id")) if compaction else "", note=selection_note, history=history,
            )
            selection_note = keep_list.get("note") or ""
            tools_added = openai_tool_definitions(added_specs)
            if keep_list != stored_list:
                _store_conversation_field(thread_id, "tool_list", keep_list)
        except Exception as exc:  # the list as offered, as in the list style
            logger.warning("Holding the tool list failed for %s: %s", thread_id, exc)
            listed_specs, tools_added = tool_specs, []
    tool_definitions = openai_tool_definitions(listed_specs) if listed_specs else None
    # The names of the user's secrets (set in the bot's computer), never the values.
    tools_prompt = describe_tools(listed_specs, secret_names=names_for(bot_id), selection_note=selection_note)
    # The titles of what the user has shown this bot; procedure_read brings one back.
    shown = procedures.prompt_block(current_bot) if tool_specs else ""
    if shown:
        tools_prompt = f"{tools_prompt}\n{shown}"
    # The bot's auto-approval mode is fixed for the whole turn, like its tools.
    gate_mode = approval_gate.mode_for(current_bot)
    gate_threshold = approval_gate.threshold_for(current_bot)
    return {
        "history": history,
        "current_bot": current_bot,
        "bot_id": bot_id,
        "system_prompt": system_prompt,
        # What a turn sends as its system prompt before any per-turn context;
        # the compaction summary request sends exactly this so its prefix is
        # served from the cache.
        "compaction_prompt": f"{system_prompt}\n\n{tools_prompt}" if tools_prompt else system_prompt,
        "llm_config": llm_config,
        "app_effort": app_effort,
        "conversation": conversation,
        "selected_model": selected_model,
        "formatted_history": formatted_history,
        "context_window": context_window,
        "tool_specs": tool_specs,
        "callable_specs": callable_specs or tool_specs,
        "tool_definitions": tool_definitions,
        "tools_prompt": tools_prompt,
        "tools_added": tools_added,
        "gate_mode": gate_mode,
        "gate_threshold": gate_threshold,
        "tool_selection": {"offered": selection.offered, "total": selection.total} if selection and selection.narrowed else None,
    }


async def _build_turn_runner(thread_id: str, requested_model: Optional[str]):
    """Capture everything a turn needs at start time and return its coroutine.

    The runner is executed by the TurnManager as its own task, so it must not
    depend on any request or connection outliving it.
    """
    setup = await _turn_setup(thread_id, requested_model)
    history = setup["history"]
    current_bot = setup["current_bot"]
    bot_id = setup["bot_id"]
    system_prompt = setup["system_prompt"]
    llm_config = setup["llm_config"]
    selected_model = setup["selected_model"]
    formatted_history = setup["formatted_history"]
    context_window = setup["context_window"]
    tool_specs = setup["tool_specs"]
    callable_specs = setup.get("callable_specs") or tool_specs
    tool_definitions = setup["tool_definitions"]
    tools_prompt = setup["tools_prompt"]
    gate_mode = setup["gate_mode"]
    gate_threshold = setup["gate_threshold"]
    replay_tools = llm_config.provider != "muapi"

    async def run(turn: Turn) -> None:
        nonlocal formatted_history, history, llm_config
        bot_msg_id = f"msg-{uuid.uuid4().hex[:6]}"
        turn_started = time.monotonic()
        stop_reason = ""
        rounds = 0
        text_pieces: List[str] = []
        # Rounds cut because the model fell into a loop (see services/repetition).
        loop_cuts = 0
        # Calls the model wrote as text (in its thinking, or unparsed in its answer) that were run,
        # and nudges sent for ones that could not be read (see services/stray_calls).
        stray_runs = 0
        stray_nudges = 0
        # Thinking is shown to the user and kept with the message for display,
        # but it is never part of `text`, so it is not replayed to the model.
        accumulated_reasoning = ""
        tool_records: List[Dict[str, Any]] = []
        # Files the bot handed to the user this turn (download cards).
        attachments: List[Dict[str, Any]] = []
        # Token usage as the provider reports it: the last round's prompt is
        # the context in use; completions add up across rounds.
        usage_prompt = 0
        usage_completion = 0
        # The last round's output alone: with the last prompt, what the context holds now
        # (earlier rounds' output is already inside that prompt; their thinking is not kept).
        usage_last_completion = 0
        usage_rounds = 0

        def usage_payload() -> Dict[str, Any]:
            return {
                "prompt_tokens": usage_prompt,
                "completion_tokens": usage_completion,
                "last_completion_tokens": usage_last_completion,
                "rounds": usage_rounds,
                "context_window": context_window,
            }
        tool_context = ""

        async def compact_if_needed(prompt_tokens: Optional[int]) -> bool:
            """Fold the stored history when a reported prompt size has outgrown
            its share of the window. True when the context was rebuilt."""
            nonlocal formatted_history, history
            if not over_budget(prompt_tokens, context_window):
                return False
            turn.emit({"type": "context.compacting", "botMsgId": bot_msg_id,
                       "promptTokens": prompt_tokens, "contextWindow": context_window})
            try:
                stored = await compact_thread(
                    thread_id, reason="auto", model=selected_model, system_prompt=setup["compaction_prompt"],
                    config=llm_config, storage=current_storage(), replay_tools=replay_tools,
                )
            except Exception as exc:  # the turn goes on with the full context; it may still fit
                logger.warning("Compaction of %s failed: %s", thread_id, exc)
                turn.emit({"type": "context.compaction_failed", "botMsgId": bot_msg_id, "error": str(exc)})
                return False
            if stored is None:
                return False
            turn.emit({"type": "context.compacted", "botMsgId": bot_msg_id, "message": stored})
            history = current_storage().get_messages(thread_id=thread_id)
            formatted_history = build_history_messages(history, replay_tools=replay_tools)
            return True

        # Stop can arrive at any await below. What the turn has produced by
        # then is kept, so the chat and the next turn both see it.
        stopped = False
        round_text = ""
        thinking_event: Optional[Dict[str, Any]] = None
        pick_id = ""
        try:
            turn.emit({"type": "turn.started", "botMsgId": bot_msg_id, "model": selected_model, **({"toolSelection": setup["tool_selection"]} if setup.get("tool_selection") else {})})

            # --- compaction: the thread has outgrown its share of the window ------
            await compact_if_needed(last_prompt_tokens(history))

            # --- how hard to think: fixed, or Auto deciding on this message (after any compaction,
            # which goes with the level the conversation last used: its prompt cache is the warm one)
            pick = await thinking.choose(current_bot, conversations.get(thread_id) or setup["conversation"], history, setup["app_effort"])
            # The level goes as reasoning_effort, or (hint style) as a sentence at the end of the message.
            sent_effort, thinking_hint = thinking.wire(pick.level)
            pick.sent, pick.hinted = sent_effort, bool(thinking_hint)
            if sent_effort != llm_config.reasoning_effort:
                llm_config = dataclasses.replace(llm_config, reasoning_effort=sent_effort)
            pick_id = thinking.record(pick, thread_id=thread_id, message_id=bot_msg_id, bot_id=bot_id, model=selected_model)
            thinking_event = pick.to_event()
            turn.emit({"type": "thinking.picked", "botMsgId": bot_msg_id, **thinking_event})
            if pick.level and (conversations.get(thread_id) or {}).get("thinking_last") != pick.level:
                try:
                    conversations.update(thread_id, {"thinking_last": pick.level})
                except Exception as exc:
                    logger.warning("Could not keep the thinking level of %s: %s", thread_id, exc)

            # --- explicit slash commands typed by the user ------------------------
            last_user_text = formatted_history[-1]["content"] if formatted_history else ""
            try:
                action_call = parse_action_command(last_user_text)
            except CommandError as exc:
                action_call = None
                tool_context = f"A {exc.tool} request was rejected before execution: {exc}"
                turn.emit({"type": "tool.failed", "tool": exc.tool, "error": str(exc)})

            if action_call:
                bot_now = live_bot(bot_id, current_bot)
                outcome = await _perform_action(
                    turn, thread_id, action_call, gate_mode=approval_gate.mode_for(bot_now), user_request=last_user_text,
                    gate_threshold=approval_gate.threshold_for(bot_now), bot_id=bot_id,
                )
                if outcome.get("status") == "completed":
                    tool_context = f"Action result ({action_call.name}): {_tool_result_text(_compact_result(outcome['result']))}"
                else:
                    tool_context = outcome.get("error") or "The action did not complete."

            turn_system_prompt = system_prompt
            if tools_prompt:
                turn_system_prompt = f"{turn_system_prompt}\n\n{tools_prompt}"
            if tool_context:
                turn_system_prompt = f"{turn_system_prompt}\n\n{tool_context}"
            # What Cubicle looks up for this message (attachment passages, the
            # bot's matching procedures, exchanges a compaction folded away) goes
            # with the message, not into the system prompt: stored on it once and
            # replayed the same on later turns, so the start of the prompt never
            # changes and the model server reuses its cache. A turn run again for
            # the same message reuses what was stored.
            # The thinking hint, and tools offered since the tool list was set (TOOL_ADD_STYLE=message),
            # are stored on the message the same way (after the slash commands above have read the
            # message as the user wrote it).
            hint_changed = _store_thinking_hint(history, thinking_hint)
            tools_changed = _store_added_tools(history, setup.get("tools_added") or [])
            attached = await attach_turn_context(turn, thread_id, bot_msg_id, history, last_user_text, bot_id=bot_id)
            if attached or hint_changed or tools_changed:
                formatted_history = build_history_messages(history, replay_tools=replay_tools)

            # --- model turn, looping while it calls tools ------------------------
            messages: List[Dict[str, Any]] = list(formatted_history)
            # A demonstration the user just recorded: its steps are in the
            # message; the frames come once, on the turn that answers it.
            demonstration = demonstrations.pending_demonstration(history)
            if demonstration and _model_accepts_images(selected_model, llm_config):
                frames = demonstrations.keyframe_message(bot_id, demonstration)
                if frames:
                    messages.append(frames)
                    turn.emit({"type": "demonstration.frames", "botMsgId": bot_msg_id, "count": sum(1 for p in frames["content"] if p.get("type") == "image_url")})
            # Where the stored history ends and this turn's own rounds begin.
            prefix_len = len(messages)
            max_rounds = max(1, settings.LLM_MAX_TOOL_ROUNDS)
            stall_limit = max(0, settings.LLM_STALLED_ROUNDS)
            rounds = 0
            # The loop ends when the model answers. Tools are withdrawn, and the
            # model gets one last call without them so the user always receives
            # an answer, when the context is full and compaction could not make
            # room, when the model only repeats itself, or at the round cap (a
            # safety net). The reason is told to the model.
            final_answer_only = False
            stop_reason = ""
            # (name, arguments) -> what the call returned, to spot rounds that
            # add nothing: every call a repeat with the same result.
            seen_results: Dict[tuple, Any] = {}
            stalled_rounds = 0

            def withdraw_tools(reason: str, tool_rounds: int) -> None:
                nonlocal final_answer_only, stop_reason
                final_answer_only = True
                stop_reason = reason
                turn.emit({"type": "turn.limit", "botMsgId": bot_msg_id, "reason": reason, "rounds": tool_rounds})

            while True:
                rounds += 1
                if rounds > 1:
                    # Between rounds the previous round has reported how big the
                    # prompt grew. A turn of many tool calls can cross the line
                    # on its own, so fold the stored history here too, and carry
                    # this turn's rounds over verbatim as the pending work.
                    if await compact_if_needed(usage_prompt):
                        messages = list(formatted_history) + messages[prefix_len:]
                        prefix_len = len(formatted_history)
                    elif not final_answer_only:
                        if at_hard_limit(usage_prompt, context_window):
                            withdraw_tools("context", rounds - 1)
                        elif stall_limit and stalled_rounds >= stall_limit:
                            withdraw_tools("stalled", rounds - 1)
                round_text = ""
                pending_calls: List[Dict[str, Any]] = []
                round_ok = True
                round_cut = False

                round_prompt = turn_system_prompt
                round_tools = tool_definitions
                if final_answer_only:
                    round_tools = None
                    round_prompt = (
                        f"{turn_system_prompt}\n\n{_STOP_PROMPTS[stop_reason]} "
                        "Do not call tools. Reply to the user now with what you found, and say what is still unverified."
                    )
                    # The list is the handoff to the next turn: name what is left.
                    left = [item for item in get_todo(thread_id) if item["state"] != "done"]
                    if left:
                        round_prompt += f" Your to-do list still has {len(left)} unfinished item(s); say which they are so the next turn can pick them up."

                round_stream = llm_service.stream_chat_completion(
                    model=selected_model,
                    messages=messages,
                    system_prompt=round_prompt,
                    config=llm_config,
                    tools=round_tools,
                )
                reply_guard = RepetitionGuard()
                thinking_guard = RepetitionGuard()
                looped: Optional[tuple] = None  # ("reply" | "thinking", what repeats)
                # A call written inside the thinking does not run; the model would go on to
                # invent its result. Stop the stream right after it and run it instead.
                call_watch = stray_calls.CallWatch() if round_tools else None
                reasoning_at_start = len(accumulated_reasoning)
                stray_cut = False
                async for event in round_stream:
                    if event["type"] == "content.delta":
                        round_text += event["delta"]
                        turn.emit({"type": "content.delta", "botMsgId": bot_msg_id, "delta": event["delta"]})
                        if reply_guard.feed(event["delta"]):
                            looped = ("reply", reply_guard.loop)
                            break
                    elif event["type"] == "reasoning.delta":
                        if call_watch is not None and call_watch.feed(event["delta"]):
                            # Keep and show only what came before the end of the call(s):
                            # anything after them is the start of an invented result.
                            kept = call_watch.kept_of(event["delta"])
                            if kept:
                                accumulated_reasoning += kept
                                turn.emit({"type": "reasoning.delta", "botMsgId": bot_msg_id, "delta": kept})
                            stray_cut = True
                            break
                        accumulated_reasoning += event["delta"]
                        turn.emit({"type": "reasoning.delta", "botMsgId": bot_msg_id, "delta": event["delta"]})
                        if thinking_guard.feed(event["delta"]):
                            looped = ("thinking", thinking_guard.loop)
                            break
                    elif event["type"] == "tool_calls":
                        # A former or habitual name (computer_write_file, write_to_file) is
                        # the tool it means, so the transcript names the real tool.
                        pending_calls = _resolve_calls(event.get("calls") or [], tool_specs, callable_specs)
                    elif event["type"] == "usage":
                        usage_rounds += 1
                        usage_prompt = int(event.get("prompt_tokens") or 0)
                        usage_completion += int(event.get("completion_tokens") or 0)
                        usage_last_completion = int(event.get("completion_tokens") or 0)
                        turn.emit({"type": "turn.usage", "botMsgId": bot_msg_id, "usage": usage_payload()})
                    elif event["type"] == "model.queued":
                        # Waiting for a slot at the model server; the chat shows the place in line.
                        turn.emit({"type": "model.queued", "botMsgId": bot_msg_id, "ahead": int(event.get("ahead") or 0), "limit": int(event.get("limit") or 0)})
                    elif event["type"] == "model.started":
                        turn.emit({"type": "model.started", "botMsgId": bot_msg_id, "waitedMs": int(event.get("waited_ms") or 0)})
                    elif event["type"] == "truncated":
                        round_cut = True
                        turn.emit({"type": "turn.truncated", "botMsgId": bot_msg_id, "reason": event.get("reason") or "length"})
                    elif event["type"] == "turn.completed":
                        round_ok = bool(event.get("ok", True))

                # Calls written as text. In the thinking: the stream was cut right after
                # them (or ended there); run them as the round's calls, and keep the
                # thinking only up to where they began. In the answer (a call the model
                # server did not parse): take them out of the text and run them.
                stray_unreadable = False
                if call_watch is not None and not pending_calls and looped is None and (stray_cut or call_watch.holds_calls()):
                    if stray_cut:
                        closer = getattr(round_stream, "aclose", None)
                        if closer is not None:
                            with contextlib.suppress(Exception):
                                await closer()
                    found, unreadable = stray_calls.parse_blocks(call_watch.text, callable_specs)
                    accumulated_reasoning = accumulated_reasoning[:reasoning_at_start] + call_watch.before_calls()
                    if found:
                        pending_calls = _resolve_calls(found, tool_specs, callable_specs, written_as_text="thinking")
                        stray_runs += len(found)
                        turn.emit({"type": "turn.stray_calls", "botMsgId": bot_msg_id, "where": "thinking", "count": len(found)})
                    elif unreadable:
                        stray_unreadable = True
                elif round_tools and not pending_calls and looped is None and stray_calls.OPEN in round_text:
                    found, unreadable = stray_calls.parse_blocks(round_text, callable_specs)
                    if found:
                        round_text = stray_calls.without_blocks(round_text)
                        pending_calls = _resolve_calls(found, tool_specs, callable_specs, written_as_text="answer")
                        stray_runs += len(found)
                        turn.emit({"type": "turn.stray_calls", "botMsgId": bot_msg_id, "where": "answer", "count": len(found)})
                if stray_unreadable and stray_nudges < _STRAY_NUDGES_MAX:
                    # A call it meant to make but wrote unreadably: say so, rather than let it guess.
                    stray_nudges += 1
                    if round_text.strip():
                        text_pieces.append(round_text)
                    messages.append({"role": "assistant", "content": round_text if round_text.strip() else "(thinking cut off at an unreadable tool call)"})
                    messages.append({"role": "user", "content": _STRAY_NUDGE})
                    turn.emit({"type": "turn.stray_calls", "botMsgId": bot_msg_id, "where": "thinking", "count": 0, "unreadable": True})
                    continue

                if looped is not None:
                    # Stop the model generating (and free its slot), keep one copy
                    # of what repeated, and tell it so. A second loop ends the turn.
                    closer = getattr(round_stream, "aclose", None)
                    if closer is not None:
                        with contextlib.suppress(Exception):
                            await closer()
                    where, sample = looped
                    loop_cuts += 1
                    pending_calls = []
                    if where == "reply":
                        round_text = _cut_loop(round_text, sample)
                    else:
                        accumulated_reasoning = _cut_loop(accumulated_reasoning, sample)
                    turn.emit({"type": "turn.loop", "botMsgId": bot_msg_id, "where": where, "sample": sample, "count": loop_cuts})
                    if round_text.strip():
                        text_pieces.append(round_text)
                    if loop_cuts >= 2 or final_answer_only:
                        text_pieces.append(_LOOP_STOP_NOTE)
                        turn.emit({"type": "content.delta", "botMsgId": bot_msg_id, "delta": f"\n\n{_LOOP_STOP_NOTE}"})
                        break
                    messages.append({"role": "assistant", "content": round_text if round_text.strip() else "(cut off)"})
                    messages.append({"role": "user", "content": _LOOP_NUDGE.format(sample=sample)})
                    if round_text.strip():
                        turn.emit({"type": "content.delta", "botMsgId": bot_msg_id, "delta": "\n\n"})
                    continue

                if round_text.strip():
                    text_pieces.append(round_text)
                # Where this round's calls sit in the reply: after the text so
                # far. The chat draws them there instead of above the reply.
                calls_at = len(_reply_text(text_pieces))
                if round_cut and not pending_calls:
                    # A text reply stopped at the output limit: say so rather
                    # than leave a sentence hanging as if it were the answer.
                    text_pieces.append(f"\n\n{_CUT_NOTE}")
                    turn.emit({"type": "content.delta", "botMsgId": bot_msg_id, "delta": f"\n\n{_CUT_NOTE}"})

                if final_answer_only:
                    if not round_text.strip():
                        note = _STOP_NOTES[stop_reason].format(rounds=max_rounds)
                        text_pieces.append(note)
                        turn.emit({"type": "content.delta", "botMsgId": bot_msg_id, "delta": note})
                    break
                if not pending_calls or not round_ok:
                    break
                if round_text.strip():
                    # Visually separate a "let me check" preamble from the answer.
                    turn.emit({"type": "content.delta", "botMsgId": bot_msg_id, "delta": "\n\n"})

                messages.append({
                    "role": "assistant",
                    "content": round_text,
                    "tool_calls": [
                        {"id": call["id"], "name": call["name"], "arguments": safe_arguments(call.get("arguments"))}
                        for call in pending_calls
                    ],
                })

                if rounds >= max_rounds:
                    # Budget spent: answer the pending calls without running
                    # them, then ask for a text-only reply.
                    for call in pending_calls:
                        tool_records.append({"id": call["id"], "name": call["name"], "status": "skipped", "at": calls_at,
                                             "raw_arguments": call.get("arguments") or "{}",
                                             "error": "Tool budget for this turn was used up."})
                        turn.emit({"type": "tool.failed", "tool": call["name"], "callName": call["name"],
                                   "error": "Not run: the tool budget for this turn was used up."})
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": _tool_result_text({"status": "skipped", "error": "Tool budget used up; answer the user now."}),
                        })
                    withdraw_tools("rounds", rounds)
                    continue

                image_messages: List[Dict[str, Any]] = []
                replies_start = len(messages)
                # The same call twice in one round (a model hedging) runs once.
                ran_this_round: Dict[tuple, str] = {}
                for call in pending_calls:
                    call_id = call["id"]
                    call_name = call["name"]
                    same_key = (call_name, json.dumps(parse_arguments_quietly(call.get("arguments")), sort_keys=True))
                    if same_key in ran_this_round:
                        note = f"Not run: the same call as {ran_this_round[same_key]} earlier in this round, which ran once; see its result."
                        tool_records.append({"id": call_id, "name": call_name, "status": "skipped", "at": calls_at, "raw_arguments": safe_arguments(call.get("arguments")), "error": note})
                        turn.emit({"type": "tool.failed", "tool": call_name, "callName": call_name, "error": note})
                        messages.append({"role": "tool", "tool_call_id": call_id, "content": _tool_result_text({"status": "skipped", "error": note})})
                        continue
                    ran_this_round[same_key] = call_id
                    record: Dict[str, Any] = {
                        "id": call_id,
                        "name": call_name,
                        "status": "running",
                        "at": calls_at,
                        # The model's own argument text, replayed verbatim in later turns
                        # (a malformed one is quoted inside a JSON object instead).
                        "raw_arguments": safe_arguments(call.get("arguments")),
                    }
                    if call.get("called_as"):
                        record["called_as"] = call["called_as"]  # the name the model used
                    if call.get("written_as_text"):
                        record["written_as_text"] = call["written_as_text"]  # recovered from its thinking or answer text
                    tool_records.append(record)

                    try:
                        arguments = parse_arguments(call.get("arguments"))
                        invocation = build_invocation(call_name, arguments, bot_id, specs=callable_specs, thread_id=thread_id)
                    except ToolCallError as exc:
                        error = str(exc)
                        if round_cut:
                            error += _CUT_HINT
                        record.update({"status": "failed", "error": error})
                        turn.emit({"type": "tool.failed", "tool": call_name, "callName": call_name, "error": error})
                        messages.append({"role": "tool", "tool_call_id": call_id, "content": _tool_result_text({"error": error})})
                        continue

                    record["summary"] = getattr(invocation, "summary", None) or getattr(invocation, "preview", call_name)
                    # The approval settings as they are now: the user may have
                    # trusted a tool, allowed a command or changed auto-approval
                    # since the turn began.
                    bot_now = live_bot(bot_id, current_bot)
                    trusted = trusted_for(bot_now, call_name, arguments, callable_specs)
                    outcome = await _perform_action(
                        turn, thread_id, invocation, call_name=call_name, gate_mode=approval_gate.mode_for(bot_now), user_request=last_user_text,
                        gate_threshold=approval_gate.threshold_for(bot_now), trusted=trusted, specs=callable_specs, bot_id=bot_id,
                    )

                    status = outcome.get("status", "failed")
                    record["status"] = status
                    if trusted and status != "rejected":
                        record["trusted"] = True
                        record["trusted_by"] = trusted if isinstance(trusted, str) else "trusted"
                    elif outcome.get("trusted_by"):
                        record["trusted"] = True
                        record["trusted_by"] = outcome["trusted_by"]
                    request_obj = outcome.get("request")
                    if request_obj is not None:
                        record["requestId"] = request_obj.request_id
                        record["arguments"] = request_obj.arguments
                    if outcome.get("gate") is not None:
                        record["gate"] = outcome["gate"].to_event()
                    if status == "completed":
                        result = outcome.get("result") or {}
                        payload = _compact_result(result)
                        if call_name == "computer_request_takeover":
                            turn.emit({
                                "type": "computer.takeover_requested",
                                "botMsgId": bot_msg_id,
                                "botId": bot_id,
                                "reason": str(arguments.get("reason") or ""),
                            })
                        if call_name == "share_file" and isinstance(result.get("attachment"), dict):
                            attachment = dict(result["attachment"])
                            if not any(a.get("source") == attachment.get("source") and a.get("path") == attachment.get("path") for a in attachments):
                                attachments.append(attachment)
                            turn.emit({"type": "attachment.added", "botMsgId": bot_msg_id, "attachment": attachment})
                        screenshot = _screenshot_message(result)
                        if screenshot is not None:
                            if _model_accepts_images(selected_model, llm_config):
                                image_messages.append(screenshot)
                                payload["image"] = "attached as the next message"
                            else:
                                payload["image"] = "omitted: the selected model does not accept images"
                        result_text = _tool_result_text(payload)
                        messages.append({"role": "tool", "tool_call_id": call_id, "content": result_text})
                        # Kept with the reply so later turns can see what came back.
                        record["result"] = trim_result_text(result_text)
                    else:
                        record["error"] = outcome.get("error")
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": _tool_result_text({"status": status, "error": outcome.get("error")}),
                        })
                # Every call above appended exactly one tool reply. A round in
                # which each call is a repeat with the same reply made no progress.
                progress = False
                for call, reply in zip(pending_calls, messages[replies_start:]):
                    key = (call["name"], call.get("arguments") or "{}")
                    if seen_results.get(key) != reply.get("content"):
                        progress = True
                    seen_results[key] = reply.get("content")
                stalled_rounds = 0 if progress else stalled_rounds + 1
                messages.extend(image_messages)
        except asyncio.CancelledError:
            stopped = True
            # Text streamed in the round that was cut off, if not yet collected.
            if round_text.strip() and round_text not in text_pieces:
                text_pieces.append(round_text)
            # A call still running (or waiting for approval) did not finish;
            # the replay to the model needs a reply for it all the same.
            for record in tool_records:
                if record.get("status") == "running":
                    record.update({"status": "cancelled", "error": "Stopped by the user before it finished."})
            note = _STOP_NOTES["cancelled"]
            text_pieces.append(note)
            turn.emit({"type": "content.delta", "botMsgId": bot_msg_id, "delta": f"\n\n{note}"})

        raw_payload: Dict[str, Any] = {}
        if accumulated_reasoning:
            raw_payload["reasoning"] = accumulated_reasoning
        if tool_records:
            cap_message_results(tool_records)
            raw_payload["tool_calls"] = tool_records
        if usage_rounds:
            raw_payload["usage"] = usage_payload()
        if loop_cuts:
            raw_payload["loop_cuts"] = loop_cuts
        if stray_runs:
            raw_payload["stray_calls"] = stray_runs
        if attachments:
            raw_payload["attachments"] = attachments
        if thinking_event is not None:
            if thinking_event["level"] or thinking_event["auto"]:
                raw_payload["thinking"] = thinking_event  # shown on the reply; nothing to show when none was sent
            thinking.finish(pick_id, {
                "duration_ms": int((time.monotonic() - turn_started) * 1000),
                "rounds": rounds,
                "completion_tokens": usage_completion,
                "tool_calls": len(tool_records),
                "stopped_early": stop_reason or None,
                "loop_cuts": loop_cuts,
                "cancelled": stopped,
            })

        bot_msg = {
            "id": bot_msg_id,
            "thread_id": thread_id,
            "bot_id": bot_id,
            "sender": "bot",
            "text": _reply_text(text_pieces),
            "created_at": datetime.now().isoformat(),
            "model": selected_model,
            "item_type": "assistant_text",
            "raw_payload": raw_payload or None,
        }
        current_storage().add_message(bot_msg)
        recall.schedule_index(thread_id, bot_msg)
        if not stopped:
            # After the first exchange a model names the conversation, in the background.
            conversation_titles.schedule(thread_id)
        if stopped:
            turn.emit({"type": "turn.cancelled", "botMsgId": bot_msg_id})
            raise asyncio.CancelledError()
        turn.emit({"type": "turn.completed", "ok": True, "botMsgId": bot_msg_id})

    return run, selected_model


def _bot_id_for(thread_id: str) -> str:
    return conversations.bot_id_of(thread_id) or thread_id


# ---- turns ------------------------------------------------------------------


@router.get("/thinking/levels")
async def thinking_levels():
    """The thinking levels to offer, and the two Auto chooses between."""
    return {
        "levels": thinking.offered_levels(),
        "light": thinking.light_level(),
        "deep": thinking.deep_level(),
        # "hint": a change of level keeps the model server's prompt cache (except to or from off).
        "style": settings.THINKING_STYLE,
    }


@router.get("/thinking/picks")
async def thinking_picks(limit: int = Query(default=200, ge=1, le=1000)):
    """The latest thinking levels chosen per turn, with how each turn went (for tuning Auto)."""
    return {"picks": thinking.recent(limit)}


@router.get("/turns")
async def list_turns():
    """Running turns and pending approvals in the caller's conversations, with their own bots and
    bots shared with them (sidebar badges); a teammate's conversations are not the owner's to see.

    `waiting` holds the approval cards from teammates' conversations this person may answer
    (a shared bot's approvers), with the bot and the person asking named."""
    from app.services.approval_broker import approval_broker
    from app.services.auth_service import auth_service
    from app.services import storage_service as storage_module

    actor = get_current_actor()
    waiting = []
    for card in approval_broker.waiting_for(actor):
        bot = next((b for b in storage_module.storage_service.for_owner(card["owner_id"]).get_bots() if b.get("id") == card.get("bot_id")), None)
        asker = auth_service.accounts.get_user(card["asked_by"]) or {}
        waiting.append({
            **{key: card.get(key) for key in ("request_id", "bot_id", "tool", "summary", "arguments", "created_at")},
            "bot_name": (bot or {}).get("name") or card.get("bot_id"),
            "asked_by_name": sharing.person_name(asker),
        })
    return {
        "turns": [turn for turn in turn_manager.overview() if sharing.thread_access(actor, turn.get("thread_id")) is not None],
        "waiting": waiting,
    }


@router.get("/turns/{thread_id}")
async def turn_status(thread_id: str):
    _ensure_thread(thread_id)
    turn = turn_manager.current(thread_id)
    return {"turn": turn.to_dict() if turn else None}


async def start_scheduled_turn(thread_id: str, message: Dict[str, Any], owner_id: Optional[str] = None) -> Turn:
    """A message not typed by the user (a schedule's run, an orchestrator's task): store it and start the turn, as its owner.

    `thread_id` is the conversation it goes to. Raises TurnBusyError while a
    reply is in progress there, so the scheduler records a skip instead of
    posting a message nobody answers.
    """
    with owner_context(owner_id) if owner_id else contextlib.nullcontext():
        current = turn_manager.current(thread_id)
        if current is not None and not current.finished:
            raise TurnBusyError(current)
        current_storage().add_message({**message, "thread_id": thread_id})
        recall.schedule_index(thread_id, message)
        runner, selected_model = await _build_turn_runner(thread_id, None)
        return turn_manager.start(thread_id, _bot_id_for(thread_id), selected_model, runner)


@router.post("/turns/{thread_id}")
async def start_turn(thread_id: str, body: Optional[StartTurnRequest] = None):
    """Start the bot's reply to the latest message as a server-owned task.

    Returns 409 with the running turn when one is already in progress, so a
    second tab or machine attaches to it instead of starting a competing one.
    """
    runner, selected_model = await _build_turn_runner(thread_id, body.model if body else None)
    try:
        turn = turn_manager.start(thread_id, _bot_id_for(thread_id), selected_model, runner)
    except TurnBusyError as exc:
        return JSONResponse(status_code=409, content={"detail": str(exc), "turn": exc.turn.to_dict()})
    return {"turn": turn.to_dict()}


@router.post("/turns/{thread_id}/cancel")
async def cancel_turn(thread_id: str):
    _ensure_thread(thread_id)
    turn = turn_manager.cancel(thread_id)
    if turn is None:
        raise HTTPException(status_code=404, detail="No running turn for this thread")
    return {"turn": turn.to_dict()}


@router.get("/stream/{thread_id}")
async def stream_turn(
    thread_id: str,
    request: Request,
    turn: Optional[str] = Query(None),
    after: int = Query(0),
):
    """Follow a turn's events over Server-Sent Events.

    Replays buffered events with seq > `after` (or the browser's Last-Event-ID
    on an automatic reconnect), then streams live until the turn finishes.
    Never starts a turn; a reconnecting client must not trigger a new reply.
    """
    _ensure_thread(thread_id)
    record = turn_manager.get(turn) if turn else turn_manager.current(thread_id)
    last_event_id = request.headers.get("last-event-id") if request is not None and request.headers else None
    if last_event_id and str(last_event_id).isdigit():
        after = max(after, int(last_event_id))

    if record is None or record.thread_id != thread_id:
        async def nothing():
            yield {"event": "message", "data": json.dumps({"type": "turn.none"})}

        return EventSourceResponse(nothing())

    async def events():
        async for event in record.subscribe(after):
            yield {"event": "message", "id": str(event["seq"]), "data": json.dumps(event)}

    return EventSourceResponse(events())
