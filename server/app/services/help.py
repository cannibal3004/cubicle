"""Cubicle's own help: the docs/ pages, for the Help view and for bots (the cubicle_help tool).

The pages ship with the code (the API image copies docs/), so they describe the version that
is running. docs/README.md is the index: its `## ` headings are the groups, and each
`- [Title](page.md): summary` line under one is a page. A page is split into sections at its
`## ` and `### ` headings for search: by meaning with the model server's embedding model when
retrieval is on (reranked when the server has a reranker), by the words in common otherwise.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services import retrieval

logger = logging.getLogger(__name__)

INDEX_PAGE = "README.md"
IMAGES = "images"
IMAGE_URL = "/api/v1/help/images/"
_LINK = re.compile(r"^\s*-\s*\[([^\]]+)\]\(([^)#\s]+\.md)\)\s*:?\s*(.*)$")
_IMAGE = re.compile(r"!\[([^\]]*)\]\((?:\.\./)*images/([^)\s]+)\)")
_WORD = re.compile(r"[a-z0-9][a-z0-9'_-]+")
_STOP = set("the a an and or of to in on for with is are be it this that you your how do does can what when where which i my me".split())


def docs_dir() -> Optional[Path]:
    """docs/ beside the code: /app/docs in the API image, the repository's docs/ when run from a checkout."""
    here = Path(__file__).resolve()
    for candidate in (here.parents[2] / "docs", here.parents[3] / "docs"):
        if (candidate / INDEX_PAGE).is_file():
            return candidate
    return None


class HelpError(ValueError):
    pass


def _page_path(path: str) -> Path:
    root = docs_dir()
    if root is None:
        raise HelpError("The help pages are not installed.")
    clean = (path or INDEX_PAGE).strip().lstrip("/")
    if not clean.endswith(".md"):
        clean += ".md"
    target = (root / clean).resolve()
    if root.resolve() not in target.parents or not target.is_file():
        raise HelpError(f"There is no help page {path!r}.")
    return target


def _title_of(text: str, fallback: str) -> str:
    return next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), fallback)


def index() -> List[Dict[str, Any]]:
    """The groups of pages, in the index's order: [{group, pages: [{path, title, summary}]}]."""
    text = _page_path(INDEX_PAGE).read_text(encoding="utf-8")
    groups: List[Dict[str, Any]] = []
    for line in text.splitlines():
        if line.startswith("## "):
            groups.append({"group": line[3:].strip(), "pages": []})
            continue
        match = _LINK.match(line)
        if match and groups:
            groups[-1]["pages"].append({"path": match.group(2), "title": match.group(1), "summary": match.group(3).strip()})
    return groups


def page(path: str) -> Dict[str, Any]:
    target = _page_path(path)
    text = target.read_text(encoding="utf-8")
    relative = target.relative_to(docs_dir().resolve()).as_posix()
    return {"path": relative, "title": _title_of(text, relative), "markdown": text}


def image(name: str) -> Path:
    root = docs_dir()
    target = (root / IMAGES / name).resolve() if root else None
    if target is None or (root / IMAGES).resolve() not in target.parents or not target.is_file():
        raise HelpError(f"There is no help image {name!r}.")
    return target


def with_image_urls(markdown: str) -> str:
    """The pages' images as URLs the chat can show (a bot's answer may include them)."""
    return _IMAGE.sub(lambda m: f"![{m.group(1)}]({IMAGE_URL}{m.group(2)})", markdown)


# ---- sections and search ---------------------------------------------------------------------

@dataclass
class Section:
    page: str
    page_title: str
    heading: str
    anchor: str
    text: str

    @property
    def title(self) -> str:
        return self.page_title if not self.heading else f"{self.page_title} › {self.heading}"

    @property
    def link(self) -> str:
        return f"/help/{self.page[:-3]}" + (f"#{self.anchor}" if self.anchor else "")

    def search_text(self) -> str:
        return f"{self.title}\n{self.text}"


def anchor_of(heading: str) -> str:
    """GitHub's anchor for a heading (the Help view gives its headings the same ids)."""
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def sections() -> List[Section]:
    root = docs_dir()
    if root is None:
        return []
    found: List[Section] = []
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root).as_posix()
        if relative == INDEX_PAGE:
            continue
        text = path.read_text(encoding="utf-8")
        title = _title_of(text, relative)
        heading, lines, in_code = "", [], False

        def close():
            body = "\n".join(lines).strip()
            if body:
                found.append(Section(relative, title, heading, anchor_of(heading) if heading else "", body))

        for line in text.splitlines():
            if line.startswith("```"):
                in_code = not in_code
            if not in_code and (line.startswith("## ") or line.startswith("### ")):
                close()
                heading, lines = line.lstrip("#").strip(), []
            elif not (not in_code and line.startswith("# ")):
                lines.append(line)
        close()
    return found


def _words(text: str) -> List[str]:
    return [w.rstrip("s") if len(w) > 4 else w for w in _WORD.findall(text.lower()) if w not in _STOP]


def keyword_rank(query: str, candidates: List[Section]) -> List[Section]:
    """By the words in common, a heading's counting three times; idf-weighted, so rare words count more."""
    wanted = set(_words(query))
    if not wanted:
        return []
    docs = [(section, _words(section.heading) * 3 + _words(section.page_title) * 2 + _words(section.text)) for section in candidates]
    count = len(docs)
    seen = {word: sum(1 for _, words in docs if word in words) for word in wanted}
    scored = []
    for section, words in docs:
        score = sum(words.count(word) * math.log(1 + count / (1 + seen[word])) for word in wanted)
        if score > 0:
            scored.append((score / (len(words) ** 0.3 or 1), section))
    return [section for _, section in sorted(scored, key=lambda item: -item[0])]


_vectors: Dict[str, List[float]] = {}  # fingerprint of a section's text -> its vector, for this process


async def search(query: str, limit: int = 4, *, config: Optional[retrieval.RetrievalConfig] = None, embed=None) -> List[Section]:
    """The sections that best answer `query`."""
    query = (query or "").strip()[: retrieval.MAX_QUERY_CHARS]
    candidates = sections()
    if not query or not candidates:
        return []
    config = config or retrieval.current_config()
    ranked: List[Section] = []
    if config.enabled:
        try:
            import numpy as np

            texts = [section.search_text() for section in candidates]
            missing = list(dict.fromkeys(t for t in texts if retrieval.fingerprint(t) not in _vectors))
            if missing:
                for text, vector in zip(missing, await (embed or retrieval.embed_texts)(missing, config)):
                    _vectors[retrieval.fingerprint(text)] = vector
            matrix = np.asarray([_vectors[retrieval.fingerprint(t)] for t in texts], dtype=np.float32)
            qv = np.asarray((await (embed or retrieval.embed_texts)([query], config))[0], dtype=np.float32)
            norms = np.linalg.norm(matrix, axis=1)
            norms[norms == 0] = 1.0
            scores = (matrix @ qv) / (norms * (float(np.linalg.norm(qv)) or 1.0))
            order = sorted(range(len(candidates)), key=lambda i: float(scores[i]), reverse=True)[: max(limit * 3, 12)]
            shortlist = [candidates[i] for i in order]
            reranked = await retrieval.rerank(query, [s.search_text() for s in shortlist], limit, config)
            ranked = [shortlist[i] for i, _ in reranked] if reranked else shortlist
        except Exception as exc:  # the model server could not embed: words in common instead
            logger.info("Help search by meaning failed, searching by words: %s", exc)
            ranked = []
    if not ranked:
        ranked = keyword_rank(query, candidates)
    return ranked[:limit]


# ---- the cubicle_help tool -------------------------------------------------------------------

SECTION_CHARS = 3000
HOW_TO_ANSWER = (
    "Answer from these sections in your own words, for this bot as it is set up (this_bot): say what to click or type, and what "
    "is already set. Link a page as [its title](its link). The ![...](/api/v1/help/images/...) images are Cubicle's own "
    "screenshots: include one, exactly as written, when it shows what you describe."
)


def bot_summary(bot_id: str, thread_id: Optional[str]) -> Dict[str, Any]:
    """How the asking bot is set up, for answers about it rather than about bots in general."""
    from app.config import settings
    from app.services import conversations, llm_service, projects, thinking
    from app.services.approval_gate import approval_gate
    from app.services.storage_service import current_storage

    bot = next((b for b in current_storage().get_bots() if b.get("id") == bot_id), None) or {}
    record = conversations.get(thread_id or bot_id) or {}
    level, source = thinking.configured(bot, record, llm_service.current_llm_config().reasoning_effort)
    tool_settings = bot.get("tool_settings") or {}
    switched_off = [f"{kind[:-1]} {name}" for kind in ("groups", "toolkits", "tools") for name, on in (tool_settings.get(kind) or {}).items() if on is False]
    project = projects.get(record["project_id"]) if record.get("project_id") else None
    return {
        "name": bot.get("name"),
        "auto_approval": f"{approval_gate.mode_for(bot)} ({'this bot' if (bot.get('auto_approval') or 'inherit') != 'inherit' else 'the app setting'})",
        "risk_threshold": approval_gate.threshold_for(bot),
        "trusted_tools": bot.get("trusted_tools") or [],
        "always_allowed_commands": bot.get("allowed_commands") or [],
        "thinking": f"{level or 'the server default'} (from {source})",
        "tools_switched_off": switched_off,
        "has_a_computer": settings.COMPUTER_PROVIDER == "docker",
        "orchestrator": bool(bot.get("orchestrator")),
        "this_conversation_project": project["name"] if project else None,
        "schedules": len(current_storage().list_schedules(bot_id)) if bot else 0,
    }


async def execute_search(call) -> Dict[str, Any]:
    question = str(call.arguments.get("question") or "")
    found = await search(question)
    result: Dict[str, Any] = {"question": question}
    if found:
        result["sections"] = [{"title": s.title, "link": s.link, "text": with_image_urls(s.text)[:SECTION_CHARS]} for s in found]
        result["how_to_answer"] = HOW_TO_ANSWER
    else:
        result["sections"] = []
        result["note"] = "Nothing in the help matches. Say so rather than guess, and point the user to Help in the sidebar (/help)."
    try:
        result["this_bot"] = bot_summary(call.arguments.get("bot_id") or "", call.arguments.get("thread_id"))
    except Exception as exc:  # the pages still answer
        logger.warning("Could not describe the bot for cubicle_help: %s", exc)
    return result


def register_help_actions(gateway=None) -> None:
    from app.services.action_gateway import ActionDefinition, action_gateway

    (gateway or action_gateway).register_action(
        ActionDefinition(name="help.search", tool="help", action="search", intent="Look something up in Cubicle's help.", risk="read", requires_approval=False),
        execute_search,
    )


register_help_actions()
