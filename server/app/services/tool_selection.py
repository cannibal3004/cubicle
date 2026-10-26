"""Which of the connected apps' tools a turn is offered.

A bot with a few connected apps and MCP servers can have a hundred or more
tool definitions, all sent on every model round; that costs context and
leads smaller models to reach for the wrong one. When the count passes the
limit (app setting `tool_selection_max`, 0 = send them all), the built-in
tools are kept as they are and the app tools are narrowed to the ones that
match the user's message: embedding similarity over "name: description",
reranked when the server has a reranker, plus any app tool the thread used
recently, so a job in progress keeps its tools.

Tool texts are embedded once per process and cached; a failure to embed
means the turn is offered everything, as before.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.config import settings
from app.services import retrieval
from app.services.llm_tools import ToolSpec

logger = logging.getLogger(__name__)

SETTING_KEY = "tool_selection_max"
_tool_vectors: Dict[str, List[float]] = {}  # fingerprint of the tool text -> vector
MAX_LIMIT = 500


def configured_max() -> int:
    from app.services.storage_service import current_storage

    value: Any = None
    try:
        value = current_storage().get_settings().get(SETTING_KEY)
    except Exception:
        value = None
    if value is None or value == "":
        value = settings.TOOL_SELECTION_MAX
    try:
        return max(0, min(MAX_LIMIT, int(value)))
    except (TypeError, ValueError):
        return max(0, min(MAX_LIMIT, settings.TOOL_SELECTION_MAX))


def tool_text(spec: ToolSpec) -> str:
    info = spec.connector or {}
    title = info.get("title") or spec.name
    toolkit = info.get("toolkit_name") or info.get("toolkit") or ""
    return f"{toolkit} {title} ({spec.name}): {spec.description}".strip()


@dataclass
class Selection:
    offered: int
    total: int
    dropped_apps: List[str]  # toolkits with no tool offered this turn

    @property
    def narrowed(self) -> bool:
        return self.offered < self.total

    def note(self) -> str:
        if not self.narrowed:
            return ""
        apps = ", ".join(self.dropped_apps[:12]) + (", …" if len(self.dropped_apps) > 12 else "")
        return (
            f"Of the connected apps' tools, the {self.offered} most relevant to the user's latest message are offered this turn"
            + (f"; apps with none offered right now: {apps}" if apps else "")
            + ". If a job needs a tool that is missing, say which app and the next turn will offer it."
        )


async def _vectors_for(texts: Sequence[str], config: retrieval.RetrievalConfig, embed=None) -> List[List[float]]:
    """Vectors for the tool texts, embedding only the ones not seen before."""
    missing = [text for text in texts if retrieval.fingerprint(text) not in _tool_vectors]
    if missing:
        unique = list(dict.fromkeys(missing))
        vectors = await (embed or retrieval.embed_texts)(unique, config)
        for text, vector in zip(unique, vectors):
            _tool_vectors[retrieval.fingerprint(text)] = vector
    return [_tool_vectors[retrieval.fingerprint(text)] for text in texts]


def recently_used(history: Iterable[Dict[str, Any]], window: int = 12) -> List[str]:
    """Tool names the thread's latest messages called."""
    names: List[str] = []
    for message in list(history)[-window:]:
        for call in ((message.get("raw_payload") or {}).get("tool_calls") or []):
            name = call.get("name")
            if isinstance(name, str) and name not in names:
                names.append(name)
    return names


async def select_tools(
    specs: List[ToolSpec],
    query: str,
    *,
    history: Optional[Iterable[Dict[str, Any]]] = None,
    max_tools: Optional[int] = None,
    config: Optional[retrieval.RetrievalConfig] = None,
    embed=None,
) -> Tuple[List[ToolSpec], Selection]:
    """The specs to offer this turn, and what was done."""
    total = len(specs)
    limit = configured_max() if max_tools is None else max(0, int(max_tools))
    builtin = [spec for spec in specs if not spec.connector]
    apps = [spec for spec in specs if spec.connector]
    everything = Selection(offered=total, total=total, dropped_apps=[])
    if limit <= 0 or total <= limit or not apps:
        return specs, everything
    config = config or retrieval.current_config()
    if not config.enabled:
        return specs, everything
    room = max(5, limit - len(builtin))
    if len(apps) <= room:
        return specs, everything

    keep_names = set(recently_used(history or []))
    kept = [spec for spec in apps if spec.name in keep_names]
    pool = [spec for spec in apps if spec.name not in keep_names]
    need = max(0, room - len(kept))
    chosen: List[ToolSpec] = []
    query = (query or "").strip()
    if need and pool and query:
        try:
            import numpy as np

            texts = [tool_text(spec) for spec in pool]
            matrix = np.asarray(await _vectors_for(texts, config, embed), dtype=np.float32)
            qv = np.asarray((await (embed or retrieval.embed_texts)([query[: retrieval.MAX_QUERY_CHARS]], config))[0], dtype=np.float32)
            norms = np.linalg.norm(matrix, axis=1)
            norms[norms == 0] = 1.0
            scores = (matrix @ qv) / (norms * (float(np.linalg.norm(qv)) or 1.0))
            order = sorted(range(len(pool)), key=lambda i: float(scores[i]), reverse=True)
            candidates = order[: min(max(need * 3, need), retrieval.RERANK_MAX_DOCUMENTS)]
            ranked = await retrieval.rerank(query, [texts[i] for i in candidates], need, config) if len(candidates) > need else None
            if ranked:
                chosen = [pool[candidates[position]] for position, _score in ranked[:need] if 0 <= position < len(candidates)]
            else:
                chosen = [pool[i] for i in candidates[:need]]
        except Exception as exc:  # embeddings unavailable: offer everything, as before
            logger.warning("Tool selection skipped: %s", exc)
            return specs, everything
    elif need and pool:
        chosen = pool[:need]  # no message to match against: the first ones, in catalog order

    offered_apps = kept + chosen
    offered_names = {spec.name for spec in offered_apps}
    # Keep the catalog's order so the prompt reads the same from turn to turn.
    selected = [spec for spec in specs if not spec.connector or spec.name in offered_names]
    present = {(spec.connector or {}).get("toolkit") for spec in offered_apps}
    dropped = []
    for spec in apps:
        toolkit = (spec.connector or {}).get("toolkit")
        if toolkit and toolkit not in present and toolkit not in dropped:
            dropped.append(toolkit)
    return selected, Selection(offered=len(selected), total=total, dropped_apps=dropped)


# ---- one selection per stretch of conversation ------------------------------------------
# The tool definitions sit at the very start of the prompt (Qwen's template puts
# them in the system block), so a list that changes from message to message
# makes the model server prefill the whole conversation again. And a message
# like "yes, go ahead" says nothing about which tools are needed. So the
# selection is made once and kept: on the first turn, and again after each
# compaction (when the prompt is rebuilt anyway). The one change in between:
# an app named in the user's message or the bot's last reply, with none of its
# tools on offer, gets its best-matching tools added ("say which app and the
# next turn will offer it").

MENTION_ADDS = 5


def mentioned_toolkits(text: str, apps: List[ToolSpec]) -> List[str]:
    """Toolkits whose slug or display name appears in `text` as a word."""
    lowered = (text or "").lower()
    if not lowered:
        return []
    found: List[str] = []
    for spec in apps:
        info = spec.connector or {}
        toolkit = str(info.get("toolkit") or "")
        if not toolkit or toolkit in found:
            continue
        names = {toolkit.lower(), str(info.get("toolkit_name") or "").lower()}
        for name in names:
            if len(name) >= 3 and re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", lowered):
                found.append(toolkit)
                break
    return found


async def _best_of(pool: List[ToolSpec], query: str, count: int, config, embed=None) -> List[ToolSpec]:
    """The `count` tools of `pool` that best match `query` (catalog order when that cannot be scored)."""
    if len(pool) <= count or not (query or "").strip():
        return pool[:count]
    try:
        import numpy as np

        texts = [tool_text(spec) for spec in pool]
        matrix = np.asarray(await _vectors_for(texts, config, embed), dtype=np.float32)
        qv = np.asarray((await (embed or retrieval.embed_texts)([query[: retrieval.MAX_QUERY_CHARS]], config))[0], dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1)
        norms[norms == 0] = 1.0
        scores = (matrix @ qv) / (norms * (float(np.linalg.norm(qv)) or 1.0))
        order = sorted(range(len(pool)), key=lambda i: float(scores[i]), reverse=True)
        return [pool[i] for i in order[:count]]
    except Exception as exc:
        logger.warning("Scoring an app's tools failed: %s", exc)
        return pool[:count]


def _selection_for(specs: List[ToolSpec], names: Iterable[str]) -> Tuple[List[ToolSpec], Selection]:
    keep = set(names)
    apps = [spec for spec in specs if spec.connector]
    offered_apps = [spec for spec in apps if spec.name in keep]
    selected = [spec for spec in specs if not spec.connector or spec.name in keep]
    present = {(spec.connector or {}).get("toolkit") for spec in offered_apps}
    dropped: List[str] = []
    for spec in apps:
        toolkit = (spec.connector or {}).get("toolkit")
        if toolkit and toolkit not in present and toolkit not in dropped:
            dropped.append(toolkit)
    return selected, Selection(offered=len(selected), total=len(specs), dropped_apps=dropped)


async def select_tools_kept(
    specs: List[ToolSpec],
    query: str,
    *,
    kept: Optional[Dict[str, Any]],
    since: str,
    mention_text: str = "",
    history: Optional[Iterable[Dict[str, Any]]] = None,
    max_tools: Optional[int] = None,
    config: Optional[retrieval.RetrievalConfig] = None,
    embed=None,
) -> Tuple[List[ToolSpec], Selection, Optional[Dict[str, Any]]]:
    """Like select_tools, but the selection holds for the stretch since `since` (the latest
    compaction's id, "" before any). `kept` is the stored selection; the third value is what
    to store now (None when nothing is narrowed)."""
    limit = configured_max() if max_tools is None else max(0, int(max_tools))
    if limit <= 0 or len(specs) <= limit or not any(spec.connector for spec in specs):
        return specs, Selection(offered=len(specs), total=len(specs), dropped_apps=[]), None
    selected, info = await select_tools(specs, query, history=history, max_tools=limit, config=config, embed=embed) if not (
        isinstance(kept, dict) and kept.get("since") == since and kept.get("limit") == limit and isinstance(kept.get("names"), list)
    ) else (None, None)
    if selected is not None:
        if not info.narrowed:
            return selected, info, None
        names = [spec.name for spec in selected if spec.connector]
        return selected, info, {"since": since, "limit": limit, "names": names}

    # The stored selection stands; add the tools of an app that was asked for by name.
    apps = [spec for spec in specs if spec.connector]
    available = {spec.name for spec in apps}
    names = [name for name in kept["names"] if name in available]
    offered_toolkits = {(spec.connector or {}).get("toolkit") for spec in apps if spec.name in set(names)}
    config = config or retrieval.current_config()
    for toolkit in mentioned_toolkits(mention_text, apps):
        if toolkit in offered_toolkits:
            continue
        pool = [spec for spec in apps if (spec.connector or {}).get("toolkit") == toolkit]
        for spec in await _best_of(pool, query or mention_text, MENTION_ADDS, config, embed):
            if spec.name not in names:
                names.append(spec.name)
        offered_toolkits.add(toolkit)
    selected, info = _selection_for(specs, names)
    return selected, info, {"since": since, "limit": limit, "names": names}
