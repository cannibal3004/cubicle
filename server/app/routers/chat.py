import asyncio
import contextlib
import json
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
from app.services.identity import get_current_owner, owner_context
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
from app.services.connector_tools import connector_tool_specs
from app.services.context_builder import build_history_messages, cap_message_results, safe_arguments, trim_result_text
from app.services.context_compactor import at_hard_limit, compact_thread, last_prompt_tokens, over_budget
from app.services import bot_memory as _bot_memory  # noqa: F401 - registers memory.read/write
from app.services.bot_todo import get_todo
from app.services.mcp_tools import mcp_tool_specs
from app.services.mcp_tools import mcp_tool_specs
from app.services.mcp_tools import mcp_tool_specs
from app.services.secrets_service import names_for, redactor_for
from app.services import attachments as attachment_service
from app.services import recall, retrieval
from app.services.tool_selection import select_tools
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
    "write long scripts or data to a file with computer_write_file (in pieces if needed) and run the file, instead of "
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


def _ensure_thread(thread_id: str) -> None:
    """Threads are bots; a bot that is not the caller's does not exist for them."""
    if not any(bot.get("id") == thread_id for bot in current_storage().get_bots()):
        raise HTTPException(status_code=404, detail="Bot not found")


@router.get("/history/{thread_id}", response_model=List[Message])
async def get_history(thread_id: str):
    _ensure_thread(thread_id)
    # Attachment text is for the model; the chat gets the card fields.
    messages = []
    for message in current_storage().get_messages(thread_id=thread_id):
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
    return {"status": "ok", "deleted": deleted}


@router.post("/send")
async def send_message(req: TurnRequest):
    _ensure_thread(req.thread_id)
    # Store user message
    user_msg = {
        "id": f"msg-{uuid.uuid4().hex[:6]}",
        "thread_id": req.thread_id,
        "bot_id": req.bot_id,
        "sender": "user",
        "text": req.user_text,
        "image_url": req.image_url,
        "created_at": datetime.now().isoformat(),
        "model": req.model or llm_service.current_llm_config().default_model,
        "item_type": "user_text"
    }
    if req.attachments:
        if len(req.attachments) > attachment_service.MAX_PER_MESSAGE:
            raise HTTPException(status_code=400, detail=f"At most {attachment_service.MAX_PER_MESSAGE} files can be attached to one message.")
        attached = []
        for ref in req.attachments:
            try:
                shared = resolve_shared_file(ref.source, ref.path, req.bot_id)
            except FileShareError as exc:
                raise HTTPException(status_code=400, detail=f"Attachment {ref.path}: {exc}")
            attached.append(await attachment_service.attach(shared, req.bot_id))
        user_msg["attachments"] = attached

    current_storage().add_message(user_msg)
    recall.schedule_index(req.bot_id, user_msg)
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
) -> Dict[str, Any]:
    """Run one governed action through the gateway, emitting events on the turn.

    Returns an outcome dict describing what happened so the caller can tell
    the model (or the system prompt) about it. `gate_mode` is the bot's
    effective auto-approval mode; `user_request` is the user's latest message,
    which the gate shows the decision model next to the proposed action.
    `trusted` means the user trusts this tool for this bot: no card, no gate.
    """
    try:
        action_request, approval = action_gateway.open(thread_id, thread_id, action_call, **({"trusted": trusted} if trusted else {}))
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

    if verdict is not None:
        decided_by = verdict.decided_by if verdict.auto_approved and decision == "allow" else "user"
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
    redact = redactor_for(thread_id)
    try:
        action_result = await action_gateway.execute(action_request)
    except ActionGatewayError as exc:
        error = redact(str(exc))
        turn.emit({"type": "tool.failed", **base, "error": error})
        return {"status": "failed", "error": f"Action could not execute ({action_name}): {error}", "request": action_request}

    if action_result.status == "completed":
        result = redact.apply(action_result.result or {})
        turn.emit({"type": "tool.completed", **base, "result": _compact_result(result)})
        return {"status": "completed", "result": result, "request": action_request, "gate": verdict}

    error = redact(action_result.error or "The action failed.")
    turn.emit({"type": "tool.failed", **base, "error": error})
    return {"status": "failed", "error": f"Action failed ({action_name}): {error}", "request": action_request, "gate": verdict}


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
        hits = await retrieval.retrieve(query, kind="attachment", ref_prefix=f"{bot_id}:", config=config)
    except Exception as exc:  # never let retrieval fail a turn
        logger.warning("Retrieval failed for %s: %s", bot_id, exc)
        return ""
    return retrieval.render_hits(
        hits,
        heading="Passages from the attached files that match this message (retrieved by similarity; the file itself holds the rest):",
    )


async def recalled_context(bot_id: str, query: str, history: List[Dict[str, Any]]) -> str:
    """Earlier exchanges of this bot that match the message and are no longer in the context; "" when none."""
    try:
        config = retrieval.current_config()
        if not config.enabled:
            return ""
        recall.ensure_backfilled(bot_id, history, config=config)
        return await recall.recall_for_turn(bot_id, query, history, config=config)
    except Exception as exc:  # never let recall fail a turn
        logger.warning("Recall failed for %s: %s", bot_id, exc)
        return ""


def describe_user() -> str:
    """One line about the person the bot works for, from their profile; empty when unset."""
    try:
        from app.services.auth_service import auth_service

        user = auth_service.accounts.get_user(get_current_owner())
    except Exception:
        return ""
    if not user:
        return ""
    name = (user.get("display_name") or "").strip()
    email = (user.get("email") or "").strip()
    if not name and not email:
        return ""
    parts = []
    if name:
        parts.append(f"The person you work for is {name} (account {user.get('username')}); address them as {name}.")
    else:
        parts.append(f"The person you work for has the account {user.get('username')}.")
    if email:
        parts.append(f"Their email address is {email}; use it when they ask to be copied, notified or contacted, without asking for it again.")
    return " ".join(parts) + "\n\n"


async def _turn_setup(thread_id: str, requested_model: Optional[str]) -> Dict[str, Any]:
    """Everything a turn (or a compaction) needs, captured once.

    The system prompt is built so that it is byte-identical from one turn to
    the next: the provider's prompt cache resumes from the end of the last
    turn only while the prefix does not change, so the date goes at the end
    and at day resolution, and nothing per-turn goes in here.
    """
    bots = current_storage().get_bots()
    current_bot = next((b for b in bots if b["id"] == thread_id), None)
    if current_bot is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    history = current_storage().get_messages(thread_id=thread_id)

    raw_prompt = current_bot["system_prompt"] if current_bot else "You are a helpful AI assistant."
    today = datetime.now().strftime("%A, %B %d, %Y")
    system_prompt = f"{raw_prompt}\n\n{describe_user()}Today is {today}."
    llm_config = llm_service.current_llm_config()
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
    selection = None
    if tools_enabled:
        # Every app connected through Composio becomes a set of tools. If the
        # catalog cannot be fetched, fall back to the two built-in GitHub tools.
        connector_specs = await connector_tool_specs(composio_service)
        # MCP servers the user runs join the same family as connected apps.
        connector_specs = connector_specs + mcp_tool_specs(bot_id=thread_id)
        tool_specs = available_tools(
            computer=settings.COMPUTER_PROVIDER == "docker",
            github=bool(composio_service.get_api_key()) and not connector_specs,
            connectors=connector_specs,
            connector_management=bool(composio_service.get_api_key()),
            orchestrator=bool(current_bot.get("orchestrator")),
        )
        # The bot's own tool choices, then apps switched off for every bot.
        tool_specs = filter_tools(
            tool_specs,
            current_bot.get("tool_settings") if current_bot else None,
            current_storage().get_settings().get("disabled_toolkits") or [],
        )
        # A bot whose computer has the shared folder at /shared uses it there.
        tool_specs = without_duplicate_routes(tool_specs)
        # The user's standing approvals for this bot.
        tool_specs = trust_tools(tool_specs, current_bot.get("trusted_tools") if current_bot else None)
        # Too many app tools for one round: keep the ones that match the message.
        latest_user = next((m for m in reversed(history) if m.get("sender") == "user" and m.get("text")), None)
        try:
            tool_specs, selection = await select_tools(tool_specs, str(latest_user.get("text") or "") if latest_user else "", history=history)
        except Exception as exc:  # never let selection fail a turn
            logger.warning("Tool selection failed for %s: %s", thread_id, exc)
            selection = None
    tool_definitions = openai_tool_definitions(tool_specs) if tool_specs else None
    # The names of the user's secrets (set in the bot's computer), never the values.
    tools_prompt = describe_tools(tool_specs, secret_names=names_for(thread_id), selection_note=selection.note() if selection else "")
    # The bot's auto-approval mode is fixed for the whole turn, like its tools.
    gate_mode = approval_gate.mode_for(current_bot)
    gate_threshold = approval_gate.threshold_for(current_bot)
    return {
        "history": history,
        "current_bot": current_bot,
        "system_prompt": system_prompt,
        # What a turn sends as its system prompt before any per-turn context;
        # the compaction summary request sends exactly this so its prefix is
        # served from the cache.
        "compaction_prompt": f"{system_prompt}\n\n{tools_prompt}" if tools_prompt else system_prompt,
        "llm_config": llm_config,
        "selected_model": selected_model,
        "formatted_history": formatted_history,
        "context_window": context_window,
        "tool_specs": tool_specs,
        "tool_definitions": tool_definitions,
        "tools_prompt": tools_prompt,
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
    system_prompt = setup["system_prompt"]
    llm_config = setup["llm_config"]
    selected_model = setup["selected_model"]
    formatted_history = setup["formatted_history"]
    context_window = setup["context_window"]
    tool_specs = setup["tool_specs"]
    tool_definitions = setup["tool_definitions"]
    tools_prompt = setup["tools_prompt"]
    gate_mode = setup["gate_mode"]
    gate_threshold = setup["gate_threshold"]
    replay_tools = llm_config.provider != "muapi"

    async def run(turn: Turn) -> None:
        nonlocal formatted_history, history
        bot_msg_id = f"msg-{uuid.uuid4().hex[:6]}"
        text_pieces: List[str] = []
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
        usage_rounds = 0

        def usage_payload() -> Dict[str, Any]:
            return {
                "prompt_tokens": usage_prompt,
                "completion_tokens": usage_completion,
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
        try:
            turn.emit({"type": "turn.started", "botMsgId": bot_msg_id, "model": selected_model, **({"toolSelection": setup["tool_selection"]} if setup.get("tool_selection") else {})})

            # --- compaction: the thread has outgrown its share of the window ------
            await compact_if_needed(last_prompt_tokens(history))

            # --- explicit slash commands typed by the user ------------------------
            last_user_text = formatted_history[-1]["content"] if formatted_history else ""
            try:
                action_call = parse_action_command(last_user_text)
            except CommandError as exc:
                action_call = None
                tool_context = f"A {exc.tool} request was rejected before execution: {exc}"
                turn.emit({"type": "tool.failed", "tool": exc.tool, "error": str(exc)})

            if action_call:
                outcome = await _perform_action(
                    turn, thread_id, action_call, gate_mode=gate_mode, user_request=last_user_text, gate_threshold=gate_threshold
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
            # Passages from long attachments that match this message: the
            # inlined part of a file is only its beginning.
            retrieved = await retrieved_context(thread_id, last_user_text)
            if retrieved:
                turn.emit({"type": "retrieval.used", "botMsgId": bot_msg_id, "count": retrieved.count("--- from ")})
                turn_system_prompt = f"{turn_system_prompt}\n\n{retrieved}"
            # Past exchanges that the compaction folded away but that match this message.
            recalled = await recalled_context(thread_id, last_user_text, history)
            if recalled:
                turn.emit({"type": "recall.used", "botMsgId": bot_msg_id, "count": recalled.count("--- from ")})
                turn_system_prompt = f"{turn_system_prompt}\n\n{recalled}"

            # --- model turn, looping while it calls tools ------------------------
            messages: List[Dict[str, Any]] = list(formatted_history)
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

                async for event in llm_service.stream_chat_completion(
                    model=selected_model,
                    messages=messages,
                    system_prompt=round_prompt,
                    config=llm_config,
                    tools=round_tools,
                ):
                    if event["type"] == "content.delta":
                        round_text += event["delta"]
                        turn.emit({"type": "content.delta", "botMsgId": bot_msg_id, "delta": event["delta"]})
                    elif event["type"] == "reasoning.delta":
                        accumulated_reasoning += event["delta"]
                        turn.emit({"type": "reasoning.delta", "botMsgId": bot_msg_id, "delta": event["delta"]})
                    elif event["type"] == "tool_calls":
                        pending_calls = list(event.get("calls") or [])
                    elif event["type"] == "usage":
                        usage_rounds += 1
                        usage_prompt = int(event.get("prompt_tokens") or 0)
                        usage_completion += int(event.get("completion_tokens") or 0)
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

                if round_text.strip():
                    text_pieces.append(round_text)
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
                        tool_records.append({"id": call["id"], "name": call["name"], "status": "skipped",
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
                for call in pending_calls:
                    call_id = call["id"]
                    call_name = call["name"]
                    record: Dict[str, Any] = {
                        "id": call_id,
                        "name": call_name,
                        "status": "running",
                        # The model's own argument text, replayed verbatim in later turns
                        # (a malformed one is quoted inside a JSON object instead).
                        "raw_arguments": safe_arguments(call.get("arguments")),
                    }
                    tool_records.append(record)

                    try:
                        arguments = parse_arguments(call.get("arguments"))
                        invocation = build_invocation(call_name, arguments, thread_id, specs=tool_specs)
                    except ToolCallError as exc:
                        error = str(exc)
                        if round_cut:
                            error += _CUT_HINT
                        record.update({"status": "failed", "error": error})
                        turn.emit({"type": "tool.failed", "tool": call_name, "callName": call_name, "error": error})
                        messages.append({"role": "tool", "tool_call_id": call_id, "content": _tool_result_text({"error": error})})
                        continue

                    record["summary"] = getattr(invocation, "summary", None) or getattr(invocation, "preview", call_name)
                    trusted = any(spec.name == call_name and spec.trusted for spec in tool_specs) or command_trusted(current_bot, call_name, arguments)
                    outcome = await _perform_action(
                        turn, thread_id, invocation, call_name=call_name, gate_mode=gate_mode, user_request=last_user_text,
                        gate_threshold=gate_threshold, trusted=trusted,
                    )

                    status = outcome.get("status", "failed")
                    record["status"] = status
                    if trusted and status != "rejected":
                        record["trusted"] = True
                        record["trusted_by"] = trusted if isinstance(trusted, str) else "trusted"
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
                                "botId": thread_id,
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
        if attachments:
            raw_payload["attachments"] = attachments

        bot_msg = {
            "id": bot_msg_id,
            "thread_id": thread_id,
            "bot_id": thread_id,
            "sender": "bot",
            "text": "\n\n".join(piece.strip("\n") for piece in text_pieces if piece.strip()),
            "created_at": datetime.now().isoformat(),
            "model": selected_model,
            "item_type": "assistant_text",
            "raw_payload": raw_payload or None,
        }
        current_storage().add_message(bot_msg)
        recall.schedule_index(thread_id, bot_msg)
        if stopped:
            turn.emit({"type": "turn.cancelled", "botMsgId": bot_msg_id})
            raise asyncio.CancelledError()
        turn.emit({"type": "turn.completed", "ok": True, "botMsgId": bot_msg_id})

    return run, selected_model


def _bot_id_for(thread_id: str) -> str:
    bots = current_storage().get_bots()
    bot = next((b for b in bots if b["id"] == thread_id), None)
    return bot["id"] if bot else thread_id


# ---- turns ------------------------------------------------------------------


@router.get("/turns")
async def list_turns():
    """Running turns and pending approvals across the caller's bots (sidebar badges)."""
    mine = {bot.get("id") for bot in current_storage().get_bots()}
    return {"turns": [turn for turn in turn_manager.overview() if turn.get("thread_id") in mine]}


@router.get("/turns/{thread_id}")
async def turn_status(thread_id: str):
    _ensure_thread(thread_id)
    turn = turn_manager.current(thread_id)
    return {"turn": turn.to_dict() if turn else None}


async def start_scheduled_turn(bot_id: str, message: Dict[str, Any], owner_id: Optional[str] = None) -> Turn:
    """A schedule's run: store its prompt as the user's message and start the turn, as its owner.

    Raises TurnBusyError while a reply is in progress, so the scheduler
    records a skip instead of posting a message nobody answers.
    """
    with owner_context(owner_id) if owner_id else contextlib.nullcontext():
        current = turn_manager.current(bot_id)
        if current is not None and not current.finished:
            raise TurnBusyError(current)
        current_storage().add_message(message)
        recall.schedule_index(bot_id, message)
        runner, selected_model = await _build_turn_runner(bot_id, None)
        return turn_manager.start(bot_id, _bot_id_for(bot_id), selected_model, runner)


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
