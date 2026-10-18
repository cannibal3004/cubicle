"""Answer a question a bot asked in the chat."""

from fastapi import APIRouter, HTTPException

from app.schemas.contracts import QuestionAnswer
from app.services import user_questions
from app.services.user_questions import MAX_ANSWER_LENGTH

router = APIRouter(prefix="/api/v1/questions", tags=["questions"])


@router.post("/answer")
async def answer_question(body: QuestionAnswer):
    answer = (body.answer or "").strip()
    if len(answer) > MAX_ANSWER_LENGTH:
        raise HTTPException(status_code=400, detail=f"Answers are limited to {MAX_ANSWER_LENGTH} characters.")
    if not answer and body.option_index is None:
        raise HTTPException(status_code=400, detail="Pick an option or type an answer.")
    if not user_questions.question_broker.answer(body.request_id, answer, body.option_index):
        raise HTTPException(status_code=404, detail="That question is no longer waiting for an answer.")
    return {"status": "ok", "request_id": body.request_id}
