"""A bot asking the user a question, with choices, and waiting for the answer.

The `ask_user` tool becomes the gateway action `user.ask`. Its executor puts
a question card in the chat through the running turn's event stream, marks
the turn as waiting (the same flag an approval card sets, so the sidebar
and a reloaded tab both know to show it), and holds the tool call open
until the user answers or the question expires. The answer comes back as
the tool result, so it is recorded with the reply like any other result.
"""

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.config import settings
from app.services.action_gateway import ActionDefinition, ActionGateway, ActionInvocation, action_gateway
from app.services.identity import get_current_owner
from app.services.turn_manager import turn_manager
from app.services.approval_broker import card_timeout

ASK_ACTION = "user.ask"
CALL_NAME = "ask_user"
MAX_OPTIONS = 6
MAX_QUESTION_LENGTH = 2000
MAX_OPTION_LENGTH = 200
MAX_ANSWER_LENGTH = 4000


class QuestionError(ValueError):
    """Raised when a question cannot be asked or answered."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class QuestionBroker:
    """Pending questions, each a future the user's answer resolves."""

    def __init__(self) -> None:
        self.pending: Dict[str, "asyncio.Future[Dict[str, Any]]"] = {}
        self.owners: Dict[str, str] = {}

    def open(self, request_id: str) -> None:
        self.pending[request_id] = asyncio.get_running_loop().create_future()
        self.owners[request_id] = get_current_owner()

    def answer(self, request_id: str, answer: str, option_index: Optional[int] = None) -> bool:
        future = self.pending.get(request_id)
        if future is None or future.done():
            return False
        if self.owners.get(request_id, get_current_owner()) != get_current_owner():
            return False  # someone else's bot asked
        future.set_result({"answer": answer, "option_index": option_index})
        return True

    async def wait(self, request_id: str, timeout: Optional[float]) -> Optional[Dict[str, Any]]:
        future = self.pending.get(request_id)
        if future is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self.pending.pop(request_id, None)
            self.owners.pop(request_id, None)


question_broker = QuestionBroker()


def _clean_options(value: Any) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise QuestionError("'options' must be a list of short strings.")
    options = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise QuestionError("Every option must be a non-empty string.")
        if len(item) > MAX_OPTION_LENGTH:
            raise QuestionError(f"Options must be at most {MAX_OPTION_LENGTH} characters.")
        options.append(item.strip())
    if len(options) > MAX_OPTIONS:
        raise QuestionError(f"At most {MAX_OPTIONS} options can be offered.")
    if len(options) == 1:
        raise QuestionError("Offer at least two options, or none for a free-text answer.")
    return options


async def execute_ask(call: ActionInvocation) -> Dict[str, Any]:
    bot_id = call.arguments.get("bot_id")
    question = str(call.arguments.get("question") or "").strip()
    if not isinstance(bot_id, str) or not bot_id or not question:
        raise QuestionError("A bot id and a question are required.")
    options = _clean_options(call.arguments.get("options"))
    allow_other = bool(call.arguments.get("allow_other", True)) or not options

    thread_id = call.arguments.get("thread_id") or bot_id
    turn = turn_manager.current(thread_id)
    if turn is None or turn.finished:
        raise QuestionError("There is no running turn to ask the user in.")

    request_id = f"q-{uuid.uuid4().hex[:10]}"
    question_broker.open(request_id)
    turn.emit({
        "type": "request.opened",
        "requestType": "question",
        "requestId": request_id,
        "tool": ASK_ACTION,
        "callName": CALL_NAME,
        "summary": question,
        "question": question,
        "options": options,
        "allowOther": allow_other,
    })
    turn.set_pending_approval({
        "request_id": request_id,
        "kind": "question",
        "tool": ASK_ACTION,
        "call_name": CALL_NAME,
        "summary": question,
        "options": options,
        "created_at": _now(),
    })
    try:
        # Questions wait as long as approval cards do (the user's setting; 0 = until answered).
        timeout = card_timeout()
        reply = await question_broker.wait(request_id, timeout)
    finally:
        turn.set_pending_approval(None)

    if reply is None:
        turn.emit({"type": "request.expired", "requestId": request_id})
        minutes = max(1, round((timeout or 0) / 60))
        return {
            "answered": False,
            "note": f"The user did not answer within {minutes} minute(s). Go on without the answer if you reasonably can; otherwise stop and say what you need.",
        }

    answer = str(reply.get("answer") or "")[:MAX_ANSWER_LENGTH]
    index = reply.get("option_index")
    result: Dict[str, Any] = {"answered": True, "answer": answer}
    if isinstance(index, int) and 0 <= index < len(options):
        result["selected_option"] = options[index]
        result["option_index"] = index
        if not answer:
            result["answer"] = options[index]
    else:
        result["typed"] = True
    turn.emit({"type": "request.answered", "requestId": request_id, "answer": result["answer"], "optionIndex": result.get("option_index")})
    return result


def register_question_actions(gateway: Optional[ActionGateway] = None) -> None:
    target = gateway or action_gateway
    target.register_action(
        ActionDefinition(
            name=ASK_ACTION,
            tool="user",
            action="ask",
            intent="Ask the user a question and wait for the answer.",
            risk="read",
            requires_approval=False,
        ),
        execute_ask,
    )


register_question_actions()
