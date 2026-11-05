"""Answer a question a bot asked in the chat."""

from fastapi import APIRouter, HTTPException

from app.schemas.contracts import QuestionAnswer
from app.services import user_questions
from app.services.user_questions import MAX_ANSWER_LENGTH

router = APIRouter(prefix="/api/v1/questions", tags=["questions"])


@router.post("/answer")
async def answer_question(body: QuestionAnswer):
    """One answer (`answer` and/or `option_index`), or `answers` for a card of several questions, in order."""
    answer = (body.answer or "").strip()
    answers = [{"answer": (a.answer or "").strip(), "option_index": a.option_index} for a in body.answers] if body.answers is not None else None
    if any(len(a["answer"]) > MAX_ANSWER_LENGTH for a in answers or [{"answer": answer}]):
        raise HTTPException(status_code=400, detail=f"Answers are limited to {MAX_ANSWER_LENGTH} characters.")
    try:
        accepted = user_questions.question_broker.answer(body.request_id, answer, body.option_index, answers=answers)
    except user_questions.QuestionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not accepted:
        raise HTTPException(status_code=404, detail="That question is no longer waiting for an answer.")
    return {"status": "ok", "request_id": body.request_id}
