"""Embeddings and reranking: finding the passages that matter for a turn.

The model server (halogen, or any OpenAI-compatible server with an
embedding model) turns text into vectors through `POST /embeddings`; an
optional reranker (`POST /rerank`, the Cohere/Jina shape halogen serves)
scores candidates against the query. Vectors live in SQLite next to the
rest of the account's data, one row per chunk, and search is a cosine pass
with numpy over the account's rows of one kind: small enough at this scale
to need no index.

Kinds in the store so far:
- "attachment": the full text of a file attached to a chat, chunked, with
  ref "<bot id>:<source>:<path>". The chat inlines only the first part of
  a long file; retrieval brings the relevant rest to each turn.

Everything here fails soft: no embedding model, a server error or a timeout
means "no retrieved context", never a failed turn.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx

from app.config import settings
from app.services.identity import get_current_owner
from app.services.npu import NpuError, post_json_with_retry

logger = logging.getLogger(__name__)

# Per attempt: an embedding batch or a rerank takes a second or two; a hung
# NPU request is cut off here and retried (see services/npu.py).
EMBED_TIMEOUT = httpx.Timeout(settings.NPU_REQUEST_TIMEOUT_SECONDS, connect=5.0)
# Items per NPU request are kept small: a rerank of 30 documents exhausted the
# NPU's device heap and hung it (2026-10-21); 20 in a request never did.
EMBED_BATCH = 16
RERANK_MAX_DOCUMENTS = 16
# The embedding model's window is small (4k tokens on halogen); a chunk and
# a query are kept well under it.
MAX_EMBED_CHARS = 6000
MAX_QUERY_CHARS = 2000
MAX_INDEX_CHARS = 2_000_000
# Only a couple of embedding calls at once: they are quick, and a burst of
# attachments must not starve the chat.
_embed_slots = asyncio.Semaphore(2)

# Embeddings supplied by tests or other callers: (texts, model) -> vectors.
EmbedFunction = Callable[[Sequence[str], Optional[str]], "asyncio.Future[List[List[float]]]"]


class RetrievalError(RuntimeError):
    pass


# ---- configuration ----------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalConfig:
    enabled: bool
    embedding_model: str  # "" lets the server pick its default
    rerank_model: str
    base_url: str
    headers: Dict[str, str] = field(default_factory=dict)
    top_k: int = 6
    chunk_chars: int = 1200


def current_config() -> RetrievalConfig:
    """What the app settings say, with the model server's connection."""
    from app.services.llm_service import current_llm_config
    from app.services.openai_compatible_service import build_headers
    from app.services.storage_service import current_storage

    try:
        values = current_storage().get_settings()
    except Exception:
        values = {}
    llm = current_llm_config()
    enabled = str(values.get("retrieval_enabled") if values.get("retrieval_enabled") is not None else settings.RETRIEVAL_ENABLED).strip().lower() in {"1", "true", "on", "yes"}
    return RetrievalConfig(
        enabled=enabled and llm.provider == "openai_compatible",
        embedding_model=str(values.get("embedding_model") or settings.EMBEDDING_MODEL or "").strip(),
        rerank_model=str(values.get("rerank_model") or settings.RERANK_MODEL or "").strip(),
        base_url=str(llm.base_url or "").rstrip("/"),
        headers=build_headers(llm),
        top_k=max(1, min(20, int(settings.RETRIEVAL_TOP_K))),
        chunk_chars=max(300, min(6000, int(settings.RETRIEVAL_CHUNK_CHARS))),
    )


# ---- the model server ----------------------------------------------------------------


async def embed_texts(texts: Sequence[str], config: Optional[RetrievalConfig] = None) -> List[List[float]]:
    """Vectors for `texts`, in order. Raises RetrievalError when the server cannot."""
    config = config or current_config()
    if not texts:
        return []
    if not config.base_url:
        raise RetrievalError("no model server configured")
    vectors: List[List[float]] = []
    async with _embed_slots:
        for start in range(0, len(texts), EMBED_BATCH):
            batch = [text[:MAX_EMBED_CHARS] for text in texts[start:start + EMBED_BATCH]]
            payload: Dict[str, Any] = {"input": batch}
            if config.embedding_model:
                payload["model"] = config.embedding_model
            try:
                response = await post_json_with_retry(f"{config.base_url}/embeddings", payload, timeout=EMBED_TIMEOUT, headers=config.headers, label="embedding model")
            except NpuError as exc:
                raise RetrievalError(str(exc)) from exc
            if response.status_code != 200:
                raise RetrievalError(f"embedding server returned {response.status_code}: {response.text[:200]}")
            try:
                data = response.json()["data"]
                ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
                vectors.extend([float(x) for x in item["embedding"]] for item in ordered)
            except (KeyError, TypeError, ValueError) as exc:
                raise RetrievalError("embedding server returned an unexpected reply") from exc
    if len(vectors) != len(texts):
        raise RetrievalError("embedding server returned the wrong number of vectors")
    return vectors


async def rerank(query: str, documents: Sequence[str], top_n: int, config: Optional[RetrievalConfig] = None) -> Optional[List[Tuple[int, float]]]:
    """(index, score) pairs best first, or None when there is no reranker to ask."""
    config = config or current_config()
    if not documents or not config.base_url:
        return None
    # Callers pass candidates best first, so the cut keeps the likeliest ones.
    documents = list(documents)[:RERANK_MAX_DOCUMENTS]
    payload: Dict[str, Any] = {"query": query[:MAX_QUERY_CHARS], "documents": [d[:MAX_EMBED_CHARS] for d in documents], "top_n": max(1, min(top_n, len(documents)))}
    if config.rerank_model:
        payload["model"] = config.rerank_model
    try:
        response = await post_json_with_retry(f"{config.base_url}/rerank", payload, timeout=EMBED_TIMEOUT, headers=config.headers, label="reranker")
    except NpuError as exc:
        logger.info("%s; keeping embedding order", exc)
        return None
    if response.status_code != 200:
        logger.info("Reranker returned %s; keeping embedding order", response.status_code)
        return None
    try:
        results = response.json()["results"]
        return [(int(item["index"]), float(item.get("relevance_score", item.get("score", 0.0)))) for item in results]
    except (KeyError, TypeError, ValueError):
        return None


# ---- chunking ------------------------------------------------------------------------


def chunk_text(text: str, size: int = 1200, overlap: int = 150) -> List[str]:
    """Pieces of about `size` characters, cut at paragraph or line breaks when
    one is near, each starting `overlap` characters before the previous end."""
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    size = max(50, int(size))
    overlap = max(0, min(int(overlap), size // 4))  # always moves forward
    if len(text) <= size:
        return [text]
    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            window = text[start:end]
            cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(". "))
            if cut > size // 2:
                end = start + cut + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


# ---- the store ----------------------------------------------------------------------


def _pack(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(blob: bytes) -> List[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))


@dataclass
class Hit:
    kind: str
    ref: str
    chunk: int
    text: str
    score: float
    meta: Dict[str, Any] = field(default_factory=dict)


class VectorIndex:
    """One account's vectors, in the shared database."""

    def __init__(self, database, owner_id: str):
        self.database = database
        self.owner_id = owner_id

    def add(self, kind: str, ref: str, chunks: Sequence[str], vectors: Sequence[Sequence[float]], *, model: str = "", meta: Optional[Dict[str, Any]] = None) -> int:
        if len(chunks) != len(vectors):
            raise RetrievalError("chunks and vectors differ in length")
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        meta_json = json.dumps(meta or {})
        with self.database.connect() as connection:
            connection.execute("DELETE FROM vectors WHERE owner_id = ? AND kind = ? AND ref = ?", (self.owner_id, kind, ref))
            connection.executemany(
                "INSERT INTO vectors(owner_id, kind, ref, chunk, text, vector, model, meta, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(self.owner_id, kind, ref, index, chunk, _pack(vector), model, meta_json, now) for index, (chunk, vector) in enumerate(zip(chunks, vectors))],
            )
        return len(chunks)

    def delete(self, kind: str, ref: str) -> None:
        with self.database.connect() as connection:
            connection.execute("DELETE FROM vectors WHERE owner_id = ? AND kind = ? AND ref = ?", (self.owner_id, kind, ref))

    def count(self, kind: Optional[str] = None, ref_prefix: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) FROM vectors WHERE owner_id = ?"
        params: List[Any] = [self.owner_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if ref_prefix:
            sql += " AND ref LIKE ?"
            params.append(ref_prefix.replace("%", "\\%") + "%")
        with self.database.connect() as connection:
            return int(connection.execute(sql, params).fetchone()[0])

    def refs(self, kind: str, ref_prefix: Optional[str] = None) -> List[str]:
        sql = "SELECT DISTINCT ref FROM vectors WHERE owner_id = ? AND kind = ?"
        params: List[Any] = [self.owner_id, kind]
        if ref_prefix:
            sql += " AND ref LIKE ?"
            params.append(ref_prefix.replace("%", "\\%") + "%")
        with self.database.connect() as connection:
            return [row[0] for row in connection.execute(sql, params).fetchall()]

    def search(self, query_vector: Sequence[float], *, kind: Optional[str] = None, ref_prefix: Optional[str] = None, limit: int = 20, newest: int = 20000) -> List[Hit]:
        """The best-matching chunks by cosine similarity, from the newest `newest` rows."""
        import numpy as np

        sql = "SELECT kind, ref, chunk, text, vector, meta FROM vectors WHERE owner_id = ?"
        params: List[Any] = [self.owner_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if ref_prefix:
            sql += " AND ref LIKE ?"
            params.append(ref_prefix.replace("%", "\\%") + "%")
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(newest))
        with self.database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        if not rows:
            return []
        query = np.asarray(query_vector, dtype=np.float32)
        norm = float(np.linalg.norm(query)) or 1.0
        query = query / norm
        dims = len(query)
        matrix = np.zeros((len(rows), dims), dtype=np.float32)
        keep: List[int] = []
        for position, row in enumerate(rows):
            vector = np.frombuffer(row[4], dtype=np.float32)
            if len(vector) != dims:
                continue  # a different model's vector: not comparable
            matrix[position] = vector
            keep.append(position)
        if not keep:
            return []
        norms = np.linalg.norm(matrix, axis=1)
        norms[norms == 0] = 1.0
        scores = (matrix @ query) / norms
        order = sorted(keep, key=lambda i: float(scores[i]), reverse=True)[: max(1, limit)]
        hits: List[Hit] = []
        for i in order:
            kind_, ref, chunk, text, _vector, meta = rows[i]
            try:
                meta_dict = json.loads(meta) if meta else {}
            except ValueError:
                meta_dict = {}
            hits.append(Hit(kind=kind_, ref=ref, chunk=int(chunk), text=text, score=float(scores[i]), meta=meta_dict))
        return hits


def index_for(storage=None) -> VectorIndex:
    from app.services.storage_service import current_storage

    storage = storage or current_storage()
    return VectorIndex(storage.database, storage.owner_id)


# ---- the two jobs: index text, retrieve for a query ------------------------------------------------


async def index_text(kind: str, ref: str, text: str, *, meta: Optional[Dict[str, Any]] = None, config: Optional[RetrievalConfig] = None, storage=None, embed: Optional[Callable] = None) -> int:
    """Chunk and embed `text` under (kind, ref), replacing what was there. Returns the chunk count."""
    config = config or current_config()
    if not config.enabled:
        return 0
    chunks = chunk_text(text[:MAX_INDEX_CHARS], size=config.chunk_chars)
    if not chunks:
        return 0
    vectors = await (embed or embed_texts)(chunks, config)
    return index_for(storage).add(kind, ref, chunks, vectors, model=config.embedding_model, meta=meta)


async def retrieve(query: str, *, kind: Optional[str] = None, ref_prefix: Optional[str] = None, limit: Optional[int] = None, config: Optional[RetrievalConfig] = None, storage=None, embed: Optional[Callable] = None, use_reranker: bool = True) -> List[Hit]:
    """The chunks most relevant to `query`: embedding search, then the reranker when there is one."""
    config = config or current_config()
    query = (query or "").strip()
    if not config.enabled or not query:
        return []
    limit = limit or config.top_k
    index = index_for(storage)
    if index.count(kind=kind, ref_prefix=ref_prefix) == 0:
        return []
    try:
        query_vector = (await (embed or embed_texts)([query[:MAX_QUERY_CHARS]], config))[0]
    except RetrievalError as exc:
        logger.warning("Retrieval skipped: %s", exc)
        return []
    candidates = index.search(query_vector, kind=kind, ref_prefix=ref_prefix, limit=min(max(limit * 3, 12), RERANK_MAX_DOCUMENTS))
    if not candidates:
        return []
    if use_reranker and len(candidates) > limit:
        ranked = await rerank(query, [hit.text for hit in candidates], limit, config)
        if ranked:
            picked = []
            for position, score in ranked[:limit]:
                if 0 <= position < len(candidates):
                    hit = candidates[position]
                    hit.score = score
                    picked.append(hit)
            return picked
    return candidates[:limit]


def render_hits(hits: Sequence[Hit], *, heading: str) -> str:
    """The retrieved passages as a block for the turn's prompt."""
    if not hits:
        return ""
    lines = [heading]
    for hit in hits:
        label = hit.meta.get("name") or hit.ref
        where = f" (part {hit.chunk + 1})"
        lines.append(f"--- from {label}{where} ---\n{hit.text.strip()}")
    return "\n".join(lines)


def fingerprint(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:16]


_WORD = re.compile(r"\w+")


def toy_embedding(text: str, dims: int = 64) -> List[float]:
    """A deterministic bag-of-words vector for tests: no model server needed."""
    vector = [0.0] * dims
    for word in _WORD.findall(text.lower()):
        if len(word) < 4:
            continue  # stop words would swamp the signal
        slot = int(hashlib.md5(word.encode()).hexdigest(), 16) % dims
        vector[slot] += 1.0
    return vector
