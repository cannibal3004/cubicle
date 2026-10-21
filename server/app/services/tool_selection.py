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
            candidates = order[: max(need * 3, need)]
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
