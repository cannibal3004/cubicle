"""A bot asking the user a question, with choices, and waiting for the answer.

The `ask_user` tool becomes the gateway action `user.ask`. Its executor puts
a question card in the chat through the running turn's event stream (one
question, or a few on one card, each answered separately and sent at once), marks
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
from app.services.identity import get_current_actor, get_current_owner
from app.services.turn_manager import turn_manager
from app.services.approval_broker import card_timeout

ASK_ACTION = "user.ask"
CALL_NAME = "ask_user"
MAX_OPTIONS = 6
MAX_QUESTION_LENGTH = 2000
MAX_OPTION_LENGTH = 200
MAX_ANSWER_LENGTH = 4000
MAX_QUESTIONS = 4


class QuestionError(ValueError):
    """Raised when a question cannot be asked or answered."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class QuestionBroker:
    """Pending questions, each a future the user's answer resolves."""

    def __init__(self) -> None:
        self.pending: Dict[str, "asyncio.Future[Dict[str, Any]]"] = {}
        self.owners: Dict[str, str] = {}
        self.actors: Dict[str, str] = {}
        self.counts: Dict[str, int] = {}

    def open(self, request_id: str, count: int = 1) -> None:
        self.pending[request_id] = asyncio.get_running_loop().create_future()
        self.owners[request_id] = get_current_owner()
        self.actors[request_id] = get_current_actor()
        self.counts[request_id] = count

    def answer(self, request_id: str, answer: str, option_index: Optional[int] = None, answers: Optional[List[Dict[str, Any]]] = None) -> bool:
        """Resolve the question; `answers` answers a card of several, one {answer, option_index} each, in order."""
        future = self.pending.get(request_id)
        if future is None or future.done():
            return False
        # A question is for the person whose turn asked it, nobody else.
        starter = self.actors.get(request_id) or self.owners.get(request_id)
        if starter and get_current_actor() != starter:
            return False
        count = self.counts.get(request_id, 1)
        if answers is None:
            answers = [{"answer": answer, "option_index": option_index}]
        if len(answers) != count or any(not str(a.get("answer") or "").strip() and a.get("option_index") is None for a in answers):
            raise QuestionError(f"Answer each of the {count} questions." if count > 1 else "Pick an option or type an answer.")
        future.set_result({"answers": answers})
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
            self.actors.pop(request_id, None)
            self.counts.pop(request_id, None)


question_broker = QuestionBroker()


def _clean_options(value: Any) -> List[Dict[str, str]]:
    """{label, description} each; a plain string is a label without a description."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise QuestionError("'options' must be a list of short choices.")
    options = []
    for item in value:
        label, description = (item, "") if isinstance(item, str) else (item.get("label"), item.get("description") or "") if isinstance(item, dict) else ("", "")
        if not isinstance(label, str) or not label.strip():
            raise QuestionError("Every option needs a label.")
        if len(label) > MAX_OPTION_LENGTH:
            raise QuestionError(f"Options must be at most {MAX_OPTION_LENGTH} characters.")
        options.append({"label": label.strip(), "description": str(description).strip()[:300]})
    if len(options) > MAX_OPTIONS:
        raise QuestionError(f"At most {MAX_OPTIONS} options can be offered.")
    if len(options) == 1:
        raise QuestionError("Offer at least two options, or none for a free-text answer.")
    return options


def _clean_questions(arguments: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The card's questions: `questions`, or the one `question` with its options (the older shape)."""
    raw = arguments.get("questions")
    if raw is None:
        raw = [{"question": arguments.get("question"), "options": arguments.get("options"), "allow_other": arguments.get("allow_other", True)}]
    if not isinstance(raw, list) or not raw or len(raw) > MAX_QUESTIONS:
        raise QuestionError(f"Ask between 1 and {MAX_QUESTIONS} questions.")
    questions = []
    for item in raw:
        text = str((item or {}).get("question") or "").strip()[:MAX_QUESTION_LENGTH]
        if not text:
            raise QuestionError("Every question needs its text.")
        options = _clean_options(item.get("options"))
        questions.append({"question": text, "header": str(item.get("header") or "")[:40], "options": options,
                          "allow_other": bool(item.get("allow_other", True)) or not options})
    return questions


def _answered(question: Dict[str, Any], reply: Dict[str, Any]) -> Dict[str, Any]:
    """One question's answer as the bot is told it."""
    labels = [o["label"] for o in question["options"]]
    answer = str(reply.get("answer") or "")[:MAX_ANSWER_LENGTH]
    index = reply.get("option_index")
    result: Dict[str, Any] = {"answer": answer}
    if isinstance(index, int) and 0 <= index < len(labels):
        result["selected_option"] = labels[index]
        result["option_index"] = index
        if not answer:
            result["answer"] = labels[index]
    else:
        result["typed"] = True
    return result


async def execute_ask(call: ActionInvocation) -> Dict[str, Any]:
    bot_id = call.arguments.get("bot_id")
    if not isinstance(bot_id, str) or not bot_id:
        raise QuestionError("A bot id and a question are required.")
    questions = _clean_questions(call.arguments)
    question = questions[0]["question"] if len(questions) == 1 else " · ".join(q["header"] or q["question"][:60] for q in questions)
    options = [o["label"] for o in questions[0]["options"]] if len(questions) == 1 else []
    allow_other = questions[0]["allow_other"] if len(questions) == 1 else True

    thread_id = call.arguments.get("thread_id") or bot_id
    turn = turn_manager.current(thread_id)
    if turn is None or turn.finished:
        raise QuestionError("There is no running turn to ask the user in.")

    request_id = f"q-{uuid.uuid4().hex[:10]}"
    question_broker.open(request_id, len(questions))
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
        "questions": [{"question": q["question"], "header": q["header"], "options": q["options"], "allowOther": q["allow_other"]} for q in questions],
    })
    turn.set_pending_approval({
        "request_id": request_id,
        "kind": "question",
        "tool": ASK_ACTION,
        "call_name": CALL_NAME,
        "summary": question,
        "options": options,
        "questions": len(questions),
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

    answers = [_answered(q, a) for q, a in zip(questions, reply.get("answers") or [])]
    if len(questions) == 1:
        result = {"answered": True, **answers[0]}
        turn.emit({"type": "request.answered", "requestId": request_id, "answer": result["answer"], "optionIndex": result.get("option_index")})
        return result
    shown = "; ".join(f"{q['header'] or q['question'][:40]}: {a['answer']}" for q, a in zip(questions, answers))
    turn.emit({"type": "request.answered", "requestId": request_id, "answer": shown})
    return {"answered": True, "answers": [{"question": q["question"], **a} for q, a in zip(questions, answers)]}


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
