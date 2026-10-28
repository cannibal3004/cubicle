import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import uuid

from app.config import settings
from app.services.identity import get_current_actor, get_current_owner
from app.services import storage_service as storage_module
from app.services.storage_service import current_storage


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PendingApproval:
    request_id: str
    future: asyncio.Future
    owner_id: str = ""
    # Whose turn opened it: the owner, or a teammate on a shared bot.
    actor_id: str = ""


def card_timeout() -> Optional[float]:
    """How long a card waits for the user (approval or question): the user's setting, else the
    environment default. None (wait until answered) when it is 0."""
    value: Any = None
    try:
        value = current_storage().get_settings().get("approval_timeout_seconds")
    except Exception:
        value = None
    if value is None or value == "":
        value = settings.APPROVAL_TIMEOUT_SECONDS
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = float(settings.APPROVAL_TIMEOUT_SECONDS)
    return None if seconds <= 0 else seconds


def may_answer(owner_id: str, actor_id: str) -> bool:
    """Whether the person acting may answer a card opened in a turn of `actor_id` on a bot of `owner_id`."""
    if not owner_id:
        return True
    me = get_current_actor()
    return me == (actor_id or owner_id) or me == owner_id


class ApprovalBroker:
    """In-process approval coordinator for the local single-user server."""

    def __init__(self):
        self.pending: Dict[str, PendingApproval] = {}

    def open(
        self,
        thread_id: str,
        bot_id: str,
        call: Any,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        request_id = request_id or f"req-{uuid.uuid4().hex[:10]}"
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending[request_id] = PendingApproval(request_id, future, get_current_owner(), get_current_actor())

        approval = {
            "request_id": request_id,
            "thread_id": thread_id,
            "bot_id": bot_id,
            "tool": call.name,
            "summary": call.summary,
            "arguments": call.arguments_for_display,
            "status": "pending",
            "created_at": _now(),
        }
        current_storage().add_approval(approval)
        current_storage().add_audit_event({
            "event": "approval.opened",
            "request_id": request_id,
            "thread_id": thread_id,
            "tool": call.name,
            "created_at": approval["created_at"],
        })
        return approval

    async def wait(self, request_id: str) -> str:
        """The user's answer, or "expired" once the card has waited as long as the user allows."""
        pending = self.pending.get(request_id)
        if not pending:
            return "expired"

        try:
            return await asyncio.wait_for(
                asyncio.shield(pending.future),
                timeout=card_timeout(),
            )
        except asyncio.TimeoutError:
            if not pending.future.done():
                pending.future.set_result("expired")
            current_storage().update_approval(request_id, {
                "status": "expired",
                "resolved_at": _now(),
            })
            current_storage().add_audit_event({
                "event": "approval.expired",
                "request_id": request_id,
                "created_at": _now(),
            })
            return "expired"
        finally:
            self.pending.pop(request_id, None)

    def resolve(self, request_id: str, action: str, decided_by: str = "user") -> bool:
        """Answer a pending approval. `decided_by` is "user" or "decider"."""
        pending = self.pending.get(request_id)
        if not pending or pending.future.done():
            return False
        # A card is answered by the account whose bot opened it, or by the
        # teammate whose turn on a shared bot it is (the gate decides inside
        # the same turn, so it matches too).
        if not may_answer(pending.owner_id, pending.actor_id):
            return False

        pending.future.set_result(action)
        storage = storage_module.storage_service.for_owner(pending.owner_id) if pending.owner_id else current_storage()
        storage.update_approval(request_id, {
            "status": action,
            "resolved_at": _now(),
            "decided_by": decided_by,
            "decided_by_user": get_current_actor(),
        })
        storage.add_audit_event({
            "event": f"approval.{action}",
            "request_id": request_id,
            "decided_by": decided_by,
            "created_at": _now(),
        })
        return True


approval_broker = ApprovalBroker()
