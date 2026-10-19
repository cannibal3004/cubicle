"""The orchestrator bot: a bot that can see the user's other bots, create
new ones and hand them work.

Delegation is a message posted into the other bot's chat, marked as coming
from the orchestrator, and a turn started there exactly as a scheduled run
starts one. The other bot keeps its own tools, computer, approvals and
history; the user sees the exchange in that bot's chat and answers any
approval or question card there. The orchestrator gets the reply back as
its tool result, waiting for it if asked to.

Every account gets one orchestrator ("Cubie") the first time its bots are
listed; any bot can be made one in its editor.
"""

import asyncio
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.config import settings
from app.services.action_gateway import ActionDefinition, ActionGateway, ActionInvocation, action_gateway
from app.services.bot_memory import get_memory
from app.services.bot_todo import get_todo
from app.services.context_builder import latest_compaction
from app.services.storage_service import StorageService, current_storage
from app.services.turn_manager import TurnBusyError, turn_manager

LIST_ACTION = "bots.list"
STATUS_ACTION = "bots.status"
CREATE_ACTION = "bots.create"
SEND_ACTION = "bots.send"
REPLY_ACTION = "bots.reply"

DEFAULT_NAME = "Cubie"
DEFAULT_AVATAR = "🧊"
WAIT_MAX_SECONDS = 1800
REPLY_MAX_CHARS = 8000
SUMMARY_MAX_CHARS = 4000
NOTES_MAX_CHARS = 2000
RECENT_MESSAGES = 6
MESSAGE_MAX_CHARS = 600
DELEGATION_MAX_CHARS = 20000

DEFAULT_PROMPT = (
    "You are Cubie, the orchestrator of this workspace. You do not do specialist work yourself when one of the user's other bots "
    "is better placed; you look at what they are doing (bots_list, bots_status), create a new bot when a kind of work needs one "
    "(bots_create: give it a clear role and a focused system prompt), hand work out (bots_send, with everything the bot needs to "
    "act on its own), and report back to the user in plain terms: what was delegated, to whom, what came back, and what still "
    "needs them. Keep the user informed rather than busy: one clear summary beats a stream of updates. When a bot waits on an "
    "approval or a question, tell the user which bot and what for."
)


class OrchestratorError(ValueError):
    """A delegation that cannot be carried out."""


# ---- the orchestrator itself ---------------------------------------------------------

def is_orchestrator(bot: Optional[Dict[str, Any]]) -> bool:
    return bool(bot and bot.get("orchestrator"))


def ensure_orchestrator(storage: Optional[StorageService] = None) -> Optional[Dict[str, Any]]:
    """Make sure the account has an orchestrator bot; returns the one created, if any."""
    store = storage or current_storage()
    bots = store.get_bots()
    if any(is_orchestrator(bot) for bot in bots):
        return None
    bot = {
        "id": f"bot-{uuid.uuid4().hex[:6]}",
        "name": DEFAULT_NAME,
        "role": "Orchestrator",
        "description": "Knows what your other bots are doing, creates new ones, and hands out work.",
        "avatar": DEFAULT_AVATAR,
        "model": settings.DEFAULT_MODEL,
        "accent_color": "#22d3ee",
        "system_prompt": DEFAULT_PROMPT,
        "tools": [],
        "tool_settings": {},
        "auto_approval": "inherit",
        "gate_threshold": None,
        "orchestrator": True,
        "pinned": True,
        "archived": False,
        "unread_count": 0,
        "created_at": datetime.now().isoformat(),
    }
    store.save_bots([bot] + bots)
    return bot


# ---- what the orchestrator sees ---------------------------------------------------------

def _manager():
    # The chat router's manager runs every turn (tests swap it there).
    from app.routers import chat as chat_router

    return getattr(chat_router, "turn_manager", turn_manager)


def _turn_state(bot_id: str) -> Dict[str, Any]:
    turn = _manager().current(bot_id)
    if turn is None or turn.finished:
        return {"state": "idle"}
    pending = turn.pending_approval or {}
    if pending:
        return {
            "state": "waiting_for_user",
            "waiting_on": "a question" if pending.get("kind") == "question" else "an approval",
            "about": pending.get("summary") or "",
        }
    return {"state": "working", "since": turn.to_dict().get("created_at")}


def _trim(text: Any, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more characters]"


def _last_activity(messages: List[Dict[str, Any]]) -> Optional[str]:
    return messages[-1].get("created_at") if messages else None


def bot_overview(bot: Dict[str, Any], storage: StorageService) -> Dict[str, Any]:
    messages = storage.get_messages(thread_id=bot["id"])
    todo = get_todo(bot["id"], storage)
    return {
        "id": bot["id"],
        "name": bot.get("name"),
        "role": bot.get("role"),
        "description": bot.get("description"),
        "model": bot.get("model"),
        "orchestrator": is_orchestrator(bot),
        "archived": bool(bot.get("archived")),
        **_turn_state(bot["id"]),
        "todo_open": sum(1 for item in todo if item.get("state") != "done"),
        "messages": len(messages),
        "last_activity": _last_activity(messages),
    }


def bot_status(bot: Dict[str, Any], storage: StorageService) -> Dict[str, Any]:
    messages = storage.get_messages(thread_id=bot["id"])
    compaction = latest_compaction(messages)
    recent = []
    for message in messages[-RECENT_MESSAGES:]:
        sender = message.get("sender")
        if sender not in ("user", "bot"):
            continue
        calls = [c.get("name") for c in ((message.get("raw_payload") or {}).get("tool_calls") or []) if isinstance(c, dict)]
        entry = {"from": sender, "at": message.get("created_at"), "text": _trim(message.get("text"), MESSAGE_MAX_CHARS)}
        if calls:
            entry["tools"] = calls[:12]
        delegated = (message.get("raw_payload") or {}).get("delegated")
        if isinstance(delegated, dict):
            entry["from"] = f"orchestrator {delegated.get('from_name') or ''}".strip()
        recent.append(entry)
    computer = None
    try:
        from app.services.computer_provider import computer_provider

        status = computer_provider.describe(bot["id"])
        computer = {"state": status.state, "url": status.url}
    except Exception:
        computer = None
    return {
        **bot_overview(bot, storage),
        "system_prompt": _trim(bot.get("system_prompt"), 1500),
        "summary": _trim(compaction.get("text"), SUMMARY_MAX_CHARS) if compaction else "",
        "summary_at": compaction.get("created_at") if compaction else None,
        "todo": get_todo(bot["id"], storage),
        "notes": _trim(get_memory(bot["id"], storage), NOTES_MAX_CHARS),
        "recent": recent,
        "computer": computer,
    }


# ---- delegating ------------------------------------------------------------------------------

def delegated_message(bot_id: str, text: str, sender: Dict[str, Any], *, message_id: Optional[str] = None) -> Dict[str, Any]:
    """The message the orchestrator posts into another bot's chat."""
    return {
        "id": message_id or f"msg-{uuid.uuid4().hex[:6]}",
        "thread_id": bot_id,
        "bot_id": bot_id,
        "sender": "user",
        "text": text,
        "created_at": datetime.now().isoformat(),
        "item_type": "user_text",
        "raw_payload": {"delegated": {"from": sender.get("id"), "from_name": sender.get("name") or DEFAULT_NAME}},
    }


def _reply_after(storage: StorageService, bot_id: str, message_id: str) -> Dict[str, Any]:
    """The bot's reply to a delegated message, as the orchestrator gets it."""
    messages = storage.get_messages(thread_id=bot_id)
    after = False
    reply = None
    for message in messages:
        if message.get("id") == message_id:
            after = True
            continue
        if after and message.get("sender") == "bot":
            reply = message
    if reply is None:
        return {"reply": "", "tools_used": []}
    calls = [c.get("name") for c in ((reply.get("raw_payload") or {}).get("tool_calls") or []) if isinstance(c, dict)]
    return {"reply": _trim(reply.get("text"), REPLY_MAX_CHARS), "tools_used": calls[:30], "replied_at": reply.get("created_at")}


async def delegate(
    sender: Dict[str, Any], target: Dict[str, Any], text: str, *, wait_seconds: float, storage: StorageService
) -> Dict[str, Any]:
    from app.routers import chat as chat_router  # the turn machinery; imported here to avoid a cycle

    message = delegated_message(target["id"], text, sender)
    try:
        turn = await chat_router.start_scheduled_turn(target["id"], message)
    except TurnBusyError:
        return {"bot": target["id"], "status": "busy", "note": f"{target.get('name')} is still working on something; try again later or check with bots_status."}
    result: Dict[str, Any] = {"bot": target["id"], "name": target.get("name"), "turn_id": turn.turn_id, "message_id": message["id"]}
    if wait_seconds <= 0:
        return {**result, "status": "started", "note": "The bot is working; use bots_reply to collect its answer later."}
    deadline = time.monotonic() + min(wait_seconds, WAIT_MAX_SECONDS)
    while time.monotonic() < deadline:
        if turn.finished:
            break
        if turn.pending_approval:
            kind = "a question" if (turn.pending_approval or {}).get("kind") == "question" else "an approval"
            return {
                **result,
                "status": "needs_user",
                "waiting_on": kind,
                "about": (turn.pending_approval or {}).get("summary") or "",
                "note": f"{target.get('name')} is waiting for the user to answer {kind} in its own chat. Tell the user; collect the reply later with bots_reply.",
            }
        await asyncio.sleep(2)
    if not turn.finished:
        return {**result, "status": "running", "note": "Still working when the wait ran out; collect the reply later with bots_reply."}
    return {**result, "status": turn.status, "error": turn.error, **_reply_after(storage, target["id"], message["id"])}


# ---- gateway executors ---------------------------------------------------------------------------

def _me(call: ActionInvocation, storage: StorageService) -> Dict[str, Any]:
    target = call.target if isinstance(call.target, dict) else {}
    bot_id = str(target.get("bot_id") or "")
    me = next((b for b in storage.get_bots() if b.get("id") == bot_id), None)
    if me is None or not is_orchestrator(me):
        raise OrchestratorError("Only an orchestrator bot can direct other bots.")
    return me


def _other(call: ActionInvocation, storage: StorageService, me: Dict[str, Any], key: str = "bot_id") -> Dict[str, Any]:
    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    bot_id = str(arguments.get(key) or "")
    bot = next((b for b in storage.get_bots() if b.get("id") == bot_id), None)
    if bot is None:
        raise OrchestratorError(f"There is no bot with id {bot_id!r}; use bots_list.")
    if bot["id"] == me["id"]:
        raise OrchestratorError("That is you.")
    return bot


async def execute_list(call: ActionInvocation) -> Dict[str, Any]:
    storage = current_storage()
    me = _me(call, storage)
    # Archived bots are listed too, flagged, so the orchestrator can say one
    # exists but is parked rather than guess or create a duplicate.
    bots = [bot_overview(b, storage) for b in storage.get_bots() if b.get("id") != me["id"]]
    return {"bots": bots, "note": "bots_status gives one bot's summary, to-do list, notes and recent messages. An archived bot is parked by the user: ask before sending it work or creating a replacement."}


async def execute_status(call: ActionInvocation) -> Dict[str, Any]:
    storage = current_storage()
    me = _me(call, storage)
    return bot_status(_other(call, storage, me), storage)


async def execute_create(call: ActionInvocation) -> Dict[str, Any]:
    storage = current_storage()
    _me(call, storage)
    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    name = str(arguments.get("name") or "").strip()
    if not name:
        raise OrchestratorError("A bot needs a name.")
    bots = storage.get_bots()
    if any(str(b.get("name") or "").lower() == name.lower() for b in bots):
        raise OrchestratorError(f"A bot named {name} already exists; use bots_list to find it.")
    from app.services.llm_service import current_llm_config

    model = str(arguments.get("model") or "").strip() or current_llm_config().default_model
    bot = {
        "id": f"bot-{uuid.uuid4().hex[:6]}",
        "name": name[:80],
        "role": str(arguments.get("role") or "AI Assistant")[:200],
        "description": str(arguments.get("description") or f"Created by the orchestrator for: {arguments.get('role') or name}")[:300],
        "avatar": str(arguments.get("avatar") or "🤖")[:16],
        "model": model,
        "accent_color": "#3b82f6",
        "system_prompt": str(arguments.get("system_prompt") or f"You are {name}, a helpful AI assistant.")[:20000],
        "tools": [],
        "tool_settings": {},
        "auto_approval": "inherit",
        "gate_threshold": None,
        "pinned": False,
        "archived": False,
        "unread_count": 0,
        "created_at": datetime.now().isoformat(),
        "created_by": "orchestrator",
    }
    storage.save_bots(bots + [bot])
    return {"bot": bot_overview(bot, storage), "note": "Created. Hand it work with bots_send; it has the usual tools and its own computer."}


async def execute_send(call: ActionInvocation) -> Dict[str, Any]:
    storage = current_storage()
    me = _me(call, storage)
    target = _other(call, storage, me)
    if is_orchestrator(target):
        raise OrchestratorError("Orchestrators do not direct each other; pick a working bot.")
    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    text = str(arguments.get("message") or "").strip()
    if not text:
        raise OrchestratorError("The message is empty.")
    if len(text) > DELEGATION_MAX_CHARS:
        raise OrchestratorError(f"The message is longer than {DELEGATION_MAX_CHARS} characters.")
    wait = arguments.get("wait_seconds")
    try:
        wait_seconds = float(wait) if wait is not None else 600.0
    except (TypeError, ValueError):
        wait_seconds = 600.0
    return await delegate(me, target, text, wait_seconds=wait_seconds, storage=storage)


async def execute_reply(call: ActionInvocation) -> Dict[str, Any]:
    storage = current_storage()
    me = _me(call, storage)
    target = _other(call, storage, me)
    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    message_id = str(arguments.get("message_id") or "")
    state = _turn_state(target["id"])
    if message_id:
        found = _reply_after(storage, target["id"], message_id)
    else:
        messages = [m for m in storage.get_messages(thread_id=target["id"]) if m.get("sender") == "bot"]
        last = messages[-1] if messages else None
        found = {"reply": _trim(last.get("text"), REPLY_MAX_CHARS) if last else "", "replied_at": last.get("created_at") if last else None}
    return {"bot": target["id"], "name": target.get("name"), **state, **found}


def register_orchestrator_actions(gateway: Optional[ActionGateway] = None) -> None:
    target = gateway or action_gateway
    target.register_action(
        ActionDefinition(name=LIST_ACTION, tool="bots", action="list", intent="List the user's bots and what they are doing.", risk="read", requires_approval=False),
        execute_list,
    )
    target.register_action(
        ActionDefinition(name=STATUS_ACTION, tool="bots", action="status", intent="Look at one bot: summary, to-do list, notes, recent messages.", risk="read", requires_approval=False),
        execute_status,
    )
    target.register_action(
        ActionDefinition(name=CREATE_ACTION, tool="bots", action="create", intent="Create a new bot.", risk="write", requires_approval=True),
        execute_create,
    )
    target.register_action(
        ActionDefinition(name=SEND_ACTION, tool="bots", action="send", intent="Give another bot a task and start its reply.", risk="write", requires_approval=True),
        execute_send,
    )
    target.register_action(
        ActionDefinition(name=REPLY_ACTION, tool="bots", action="reply", intent="Collect another bot's latest reply.", risk="read", requires_approval=False),
        execute_reply,
    )


register_orchestrator_actions()
