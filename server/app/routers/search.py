"""Semantic search over the account's conversations, attachments and notes."""

from fastapi import APIRouter, HTTPException, Query

from app.services import conversations, sharing
from app.services import search as search_service
from app.services.identity import get_current_actor
from app.services.storage_service import current_storage

router = APIRouter(prefix="/api/v1/search", tags=["search"])


@router.get("")
async def search(q: str = Query("", max_length=2000), bot_id: str = Query(""), limit: int = Query(12, ge=1, le=50)):
    """Results best first: messages, attachment passages and notes that match `q`."""
    query = q.strip()
    if not query:
        return {"query": "", "results": []}
    bots = current_storage().get_bots()
    if bot_id and not any(b.get("id") == bot_id for b in bots):
        raise HTTPException(status_code=404, detail="No such bot.")
    try:
        results = await search_service.search(query, bot_id=bot_id or None, limit=limit, bots=bots)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Search is unavailable: {exc}")
    # Messages from a teammate's conversations with one of the caller's shared bots stay theirs.
    storage, actor = current_storage(), get_current_actor()

    def visible(result):
        if result.get("kind") != "message":
            return True
        record = conversations.get(str(result.get("bot_id") or ""), storage)
        return record is None or sharing.visible(record, actor, storage.owner_id)

    return {"query": query, "results": [r for r in results if visible(r)]}
