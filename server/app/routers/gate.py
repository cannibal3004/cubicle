"""The auto-approval gate: test a decision server, review what it decided."""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.approval_gate import QUESTIONS, approval_gate
from app.services.storage_service import current_storage

router = APIRouter(prefix="/api/v1/gate", tags=["gate"])


class GateCheckRequest(BaseModel):
    # Empty means "the saved decider_url".
    decider_url: str = Field(default="", max_length=300, pattern=r"^(https?://[^\s]+)?$")


@router.post("/check")
async def check_decider(body: Optional[GateCheckRequest] = None):
    """Send one harmless probe to the decision server and report its answer."""
    url = (body.decider_url if body else "").strip() or str(current_storage().get_settings().get("decider_url") or "").strip()
    if not url:
        raise HTTPException(status_code=422, detail="No decision server URL is configured.")
    return {"decider_url": url, **(await approval_gate.check(url))}


@router.get("/decisions")
async def list_decisions(limit: int = Query(200, ge=1, le=5000)):
    """What the gate was shown and concluded, oldest first, with the user's final answer."""
    return {"questions": QUESTIONS, "decisions": current_storage().get_gate_decisions(limit)}
