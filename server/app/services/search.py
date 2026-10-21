"""Search across an account's conversations, attachments and notes; bots ranked for a task.

Both ride on the retrieval store. Messages and attachments are indexed as
they arrive (recall, attachments); a bot's notes are indexed whenever they
are saved, under kind "notes" with the bot id as ref. Search embeds the
query once, gathers the best chunks of each kind, reranks them together
when the server has a reranker, and returns snippets with enough context
(bot, kind, when) for a result list.

Ranking bots for a task embeds each bot's name, role, description, prompt
and notes (cached per process by content) and scores them against the
task, so an orchestrator can see which bot fits before delegating.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.services import retrieval
from app.services.identity import get_current_owner, owner_context

logger = logging.getLogger(__name__)

NOTES_KIND = "notes"
SEARCH_KINDS = ("message", "attachment", NOTES_KIND)
SNIPPET_CHARS = 320
_bot_vectors: Dict[str, List[float]] = {}


# ---- notes ----------------------------------------------------------------------------


def schedule_notes_index(bot_id: str, bot_name: str, text: str) -> None:
    """Embed a bot's notes in the background after a save (empty notes clear the index)."""
    try:
        config = retrieval.current_config()
    except Exception:
        return
    if not config.enabled:
        return
    owner = get_current_owner()

    async def run() -> None:
        with owner_context(owner):
            try:
                if text.strip():
                    await retrieval.index_text(NOTES_KIND, bot_id, text, meta={"name": f"notes of {bot_name}", "bot_id": bot_id}, config=config)
                else:
                    retrieval.index_for().delete(NOTES_KIND, bot_id)
            except Exception as exc:
                logger.warning("Could not index the notes of %s: %s", bot_id, exc)

    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        pass


# ---- search ----------------------------------------------------------------------------


def _bot_of(hit: retrieval.Hit) -> str:
    if hit.kind == NOTES_KIND:
        return hit.ref
    return hit.ref.split(":", 1)[0]


def _snippet(text: str, query: str) -> str:
    """A window of the chunk around the first query word that appears, else its start."""
    flat = " ".join(text.split())
    if len(flat) <= SNIPPET_CHARS:
        return flat
    lower = flat.lower()
    at = -1
    for word in sorted((w for w in query.lower().split() if len(w) > 3), key=len, reverse=True):
        at = lower.find(word)
        if at >= 0:
            break
    if at < 0:
        return flat[:SNIPPET_CHARS].rstrip() + "…"
    start = max(0, at - SNIPPET_CHARS // 3)
    end = min(len(flat), start + SNIPPET_CHARS)
    return ("…" if start > 0 else "") + flat[start:end].strip() + ("…" if end < len(flat) else "")


async def search(query: str, *, bot_id: Optional[str] = None, limit: int = 12, kinds: Sequence[str] = SEARCH_KINDS, config: Optional[retrieval.RetrievalConfig] = None, storage=None, embed=None, bots: Optional[Iterable[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Results across kinds, best first, as plain dicts for the API."""
    config = config or retrieval.current_config()
    query = (query or "").strip()
    if not config.enabled or not query:
        return []
    index = retrieval.index_for(storage)
    try:
        query_vector = (await (embed or retrieval.embed_texts)([query[: retrieval.MAX_QUERY_CHARS]], config))[0]
    except retrieval.RetrievalError as exc:
        logger.warning("Search skipped: %s", exc)
        return []
    candidates: List[retrieval.Hit] = []
    for kind in kinds:
        prefix = None
        if bot_id:
            prefix = bot_id if kind == NOTES_KIND else f"{bot_id}:"
        if index.count(kind=kind, ref_prefix=prefix) == 0:
            continue
        candidates.extend(index.search(query_vector, kind=kind, ref_prefix=prefix, limit=max(limit * 3, 15)))
    if not candidates:
        return []
    candidates.sort(key=lambda hit: hit.score, reverse=True)
    pool = candidates[: max(limit * 4, 30)]
    ranked = await retrieval.rerank(query, [hit.text for hit in pool], limit, config) if len(pool) > limit else None
    if ranked:
        picked = []
        for position, score in ranked[:limit]:
            if 0 <= position < len(pool):
                pool[position].score = score
                picked.append(pool[position])
        chosen = picked
    else:
        chosen = pool[:limit]
    names = {b.get("id"): b.get("name") for b in (bots or [])}
    results: List[Dict[str, Any]] = []
    seen = set()
    for hit in chosen:
        owner_bot = _bot_of(hit)
        # The same passage twice (a repeated message, a re-attached file) is one result.
        key = (hit.kind, owner_bot, " ".join(hit.text.split())[:400])
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "kind": hit.kind,
            "bot_id": owner_bot,
            "bot_name": names.get(owner_bot) or hit.meta.get("bot_name") or "",
            "ref": hit.ref,
            "chunk": hit.chunk,
            "message_id": hit.meta.get("message_id"),
            "sender": hit.meta.get("sender"),
            "created_at": hit.meta.get("created_at"),
            "name": hit.meta.get("name") or ("notes" if hit.kind == NOTES_KIND else hit.ref),
            "path": hit.meta.get("path"),
            "source": hit.meta.get("source"),
            "snippet": _snippet(hit.text, query),
            "score": round(float(hit.score), 4),
        })
    return results


# ---- bots for a task ----------------------------------------------------------------------


def bot_text(bot: Dict[str, Any], notes: str = "") -> str:
    parts = [str(bot.get("name") or ""), str(bot.get("role") or ""), str(bot.get("description") or ""), str(bot.get("system_prompt") or "")[:1500]]
    if notes:
        parts.append("Notes: " + notes[:1500])
    return ". ".join(p for p in parts if p)


async def rank_bots(bots: List[Dict[str, Any]], task: str, *, notes: Optional[Dict[str, str]] = None, config: Optional[retrieval.RetrievalConfig] = None, embed=None) -> List[Tuple[Dict[str, Any], float]]:
    """(bot, similarity) best first; [] when ranking is not possible."""
    config = config or retrieval.current_config()
    task = (task or "").strip()
    if not config.enabled or not task or not bots:
        return []
    import numpy as np

    texts = [bot_text(bot, (notes or {}).get(bot.get("id"), "")) for bot in bots]
    missing = [t for t in texts if retrieval.fingerprint(t) not in _bot_vectors]
    try:
        if missing:
            unique = list(dict.fromkeys(missing))
            for text, vector in zip(unique, await (embed or retrieval.embed_texts)(unique, config)):
                _bot_vectors[retrieval.fingerprint(text)] = vector
        qv = np.asarray((await (embed or retrieval.embed_texts)([task[: retrieval.MAX_QUERY_CHARS]], config))[0], dtype=np.float32)
    except retrieval.RetrievalError as exc:
        logger.warning("Bot ranking skipped: %s", exc)
        return []
    matrix = np.asarray([_bot_vectors[retrieval.fingerprint(t)] for t in texts], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0] = 1.0
    scores = (matrix @ qv) / (norms * (float(np.linalg.norm(qv)) or 1.0))
    order = sorted(range(len(bots)), key=lambda i: float(scores[i]), reverse=True)
    return [(bots[i], float(scores[i])) for i in order]
