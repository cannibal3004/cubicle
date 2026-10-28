"""Files a user attaches to a message.

An upload is saved where the bot can reach it with its ordinary tools: the
bot's computer workspace (its /workspace, in bind mode) or else the shared
folder. The chat shows it as the same download card a bot's share_file
produces, and the model gets the file's text, converted from PDF and Office
formats with MarkItDown and capped so one big document cannot fill the
context; above the cap the model is told where the whole file is.
"""

import asyncio
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import logging

from app.config import settings
from app.services import retrieval
from app.services.identity import get_current_actor, get_current_owner, owner_context

logger = logging.getLogger(__name__)
from app.services.computer_provider import computer_id_for_bot
from app.services.file_share import FileShareError, SharedFile, resolve_shared_file

UPLOADS_DIR = "uploads"
MAX_PER_MESSAGE = 10
MAX_NAME_LENGTH = 120

# Read as they are: the model handles these better than a Markdown rendering
# of them, and nothing is lost.
PLAIN_TEXT_SUFFIXES = {
    ".txt", ".text", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".jsonl", ".ndjson",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".log", ".xml", ".svg",
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".sh", ".bash", ".ps1", ".bat",
    ".sql", ".r", ".rb", ".go", ".rs", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs",
    ".php", ".pl", ".lua", ".swift", ".css", ".scss", ".tex", ".bib", ".diff", ".patch",
    ".env", ".gitignore", ".dockerfile", ".makefile",
}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp", ".tif", ".tiff", ".heic"}


class AttachmentError(ValueError):
    """Raised when a file cannot be attached."""


@dataclass(frozen=True)
class Extract:
    text: str
    chars: int  # of the whole conversion, before the cap
    truncated: bool
    note: str  # how the text was obtained, or why there is none


def upload_target(bot_id: Optional[str]) -> Tuple[str, Path]:
    """Where a file for this bot goes: its computer workspace when there is
    one on disk, else the shared folder."""
    if bot_id and settings.COMPUTER_PROVIDER == "docker" and settings.COMPUTER_DOCKER_WORKSPACE_MODE == "bind":
        return "computer", settings.COMPUTER_DOCKER_WORKSPACE_ROOT / computer_id_for_bot(bot_id)
    return "workspace", settings.WORKSPACE_ROOT


def safe_filename(name: str) -> str:
    """A plain file name: no directories, no control characters, a bounded
    length, and the suffix kept so converters recognise the type."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = unicodedata.normalize("NFKC", base)
    base = "".join(ch for ch in base if ch.isprintable() and ch not in '<>:"|?*')
    base = re.sub(r"\s+", " ", base).strip(" .")
    if not base:
        base = "file"
    if len(base) > MAX_NAME_LENGTH:
        stem, dot, suffix = base.rpartition(".")
        if dot and 0 < len(suffix) <= 12:
            base = stem[: MAX_NAME_LENGTH - len(suffix) - 1].rstrip(" .") + "." + suffix
        else:
            base = base[:MAX_NAME_LENGTH].rstrip(" .")
    return base


def _world_writable(path: Path, mode: int) -> None:
    # The computer runs as a different unprivileged user than the API, so
    # what the API writes into a bind-mounted workspace must be open to it.
    try:
        path.chmod(mode)
    except OSError:
        pass


def save_upload(bot_id: Optional[str], filename: str, data: bytes) -> Dict[str, Any]:
    """Store an uploaded file and return its attachment record."""
    if not data:
        raise AttachmentError("The file is empty.")
    if len(data) > settings.ATTACHMENT_MAX_BYTES:
        raise AttachmentError(f"The file is larger than the {settings.ATTACHMENT_MAX_BYTES // (1024 * 1024)} MB attachment limit.")
    source, root = upload_target(bot_id)
    folder = root / UPLOADS_DIR
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AttachmentError(f"Could not create the uploads folder: {exc}") from exc
    _world_writable(root, 0o777)
    _world_writable(folder, 0o777)
    name = safe_filename(filename)
    target = folder / name
    stem, dot, suffix = name.rpartition(".")
    if not dot or not stem:
        stem, suffix = name, ""
    counter = 2
    while target.exists():
        target = folder / (f"{stem} ({counter}).{suffix}" if suffix else f"{stem} ({counter})")
        counter += 1
    target.write_bytes(data)
    _world_writable(target, 0o666)
    try:
        shared = resolve_shared_file(source, f"{UPLOADS_DIR}/{target.name}", bot_id)
    except FileShareError as exc:
        raise AttachmentError(str(exc)) from exc
    return describe(shared, bot_id)


def location_for(source: str, path: str) -> str:
    """Where the model should look for the file, in its own terms."""
    if source == "computer":
        return f"/workspace/{path}"
    if settings.COMPUTER_PROVIDER == "docker" and settings.COMPUTER_DOCKER_HOST_SHARED_DIR:
        return f"/shared/{path} (the shared folder)"
    return f"{path} in the shared folder (shared_folder_read)"


def describe(shared: SharedFile, bot_id: Optional[str]) -> Dict[str, Any]:
    record = shared.to_attachment()
    record["location"] = location_for(shared.source, shared.path)
    return record


# ---- text extraction ----------------------------------------------------------

def _plain_text(path: Path) -> Optional[str]:
    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        return None  # binary, whatever the suffix says
    return data.decode("utf-8", errors="replace")


def extract_text(path: Path, limit: Optional[int] = None) -> Extract:
    """The file's text, capped at `limit` characters. Never raises: a file
    that cannot be read as text comes back with an empty text and a note."""
    cap = settings.ATTACHMENT_INLINE_MAX_CHARS if limit is None else limit
    suffix = path.suffix.lower()
    text: Optional[str] = None
    note = ""
    if suffix in IMAGE_SUFFIXES:
        note = "an image file; it is not shown to you as text"
    elif suffix in PLAIN_TEXT_SUFFIXES or not suffix:
        text = _plain_text(path)
        note = "read as text" if text is not None else "binary data; no text to show"
    else:
        try:
            from markitdown import MarkItDown  # imported here: it is slow to load and optional

            result = MarkItDown(enable_plugins=False).convert(str(path))
            text = result.text_content or ""
            note = "converted to text with MarkItDown"
        except ImportError:
            text = _plain_text(path)
            note = "read as text (MarkItDown is not installed)" if text is not None else "MarkItDown is not installed, so no text was extracted"
        except Exception as exc:  # unsupported or corrupt file
            text = _plain_text(path) if suffix not in {".zip", ".gz", ".tgz", ".7z", ".rar", ".tar"} else None
            if text is not None and text.strip():
                note = f"read as text (conversion failed: {type(exc).__name__})"
            else:
                text = None
                note = f"no text could be extracted ({type(exc).__name__})"
    if text is None:
        return Extract(text="", chars=0, truncated=False, note=note)
    text = text.strip()
    total = len(text)
    truncated = total > cap
    if truncated:
        text = text[:cap]
    return Extract(text=text, chars=total, truncated=truncated, note=note)


async def attach(shared: SharedFile, bot_id: Optional[str]) -> Dict[str, Any]:
    """The attachment record for a message: card fields, location and the
    text for the model (extracted in a worker thread)."""
    record = describe(shared, bot_id)
    # Extracted once in full: the message inlines the first part, and the
    # whole text is indexed for retrieval when embeddings are available.
    try:
        full = await asyncio.wait_for(asyncio.to_thread(extract_text, shared.absolute, retrieval.MAX_INDEX_CHARS), timeout=settings.ATTACHMENT_EXTRACT_TIMEOUT)
    except asyncio.TimeoutError:
        full = Extract(text="", chars=0, truncated=False, note=f"text extraction did not finish within {settings.ATTACHMENT_EXTRACT_TIMEOUT:.0f}s")
    except Exception as exc:  # pragma: no cover - extract_text should not raise
        full = Extract(text="", chars=0, truncated=False, note=f"no text could be extracted ({type(exc).__name__})")
    cap = settings.ATTACHMENT_INLINE_MAX_CHARS
    extract = Extract(text=full.text[:cap], chars=full.chars, truncated=full.chars > cap, note=full.note)
    record["extract"] = asdict(extract)
    if full.text and bot_id:
        schedule_indexing(bot_id, record, full.text)
    return record


def attachment_ref(bot_id: str, attachment: Dict[str, Any]) -> str:
    return f"{bot_id}:{attachment.get('source')}:{attachment.get('path')}"


def schedule_indexing(bot_id: str, attachment: Dict[str, Any], text: str) -> None:
    """Index the attachment's text for retrieval without holding up the message."""
    try:
        config = retrieval.current_config()
    except Exception:
        return
    if not config.enabled:
        return
    # Who attached it: on a shared bot, passages come back only in that person's conversations.
    meta = {"name": attachment.get("name"), "bot_id": bot_id, "source": attachment.get("source"), "path": attachment.get("path"), "user_id": get_current_actor()}
    owner = get_current_owner()

    async def run() -> None:
        with owner_context(owner):
            try:
                count = await retrieval.index_text("attachment", attachment_ref(bot_id, attachment), text, meta=meta, config=config)
                logger.info("Indexed %s in %d chunks for retrieval", attachment.get("name"), count)
            except Exception as exc:  # retrieval is a nicety; the attachment itself is already saved
                logger.warning("Could not index %s for retrieval: %s", attachment.get("name"), exc)

    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        pass


def _size(bytes_count: int) -> str:
    if bytes_count < 1024:
        return f"{bytes_count} B"
    if bytes_count < 1024 * 1024:
        return f"{bytes_count / 1024:.1f} KB"
    return f"{bytes_count / (1024 * 1024):.1f} MB"


def for_model(attachment: Dict[str, Any]) -> str:
    """How an attachment appears inside the user's message to the model."""
    name = str(attachment.get("name") or "file")
    header = f"[Attached file: {name} ({_size(int(attachment.get('size') or 0))}, {attachment.get('mime') or 'unknown type'}). Saved at {attachment.get('location') or attachment.get('path')}; use your file and terminal tools there to work with the whole file.]"
    extract = attachment.get("extract") or {}
    text = str(extract.get("text") or "")
    note = str(extract.get("note") or "")
    if not text:
        return f"{header}\n(No text is shown: {note or 'nothing could be extracted'}.)"
    chars = int(extract.get("chars") or len(text))
    if extract.get("truncated"):
        shown = f"showing the first {len(text):,} of {chars:,} characters; the rest is only in the file"
    else:
        shown = f"{chars:,} characters"
    return f"{header}\n--- {name} ({note}; {shown}) ---\n{text}\n--- end of {name} ---"


def without_text(attachments: Any) -> Any:
    """Attachment records as the API returns them: card fields only."""
    if not isinstance(attachments, list):
        return attachments
    cleaned = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        record = {k: v for k, v in item.items() if k != "extract"}
        extract = item.get("extract")
        if isinstance(extract, dict):
            record["text_chars"] = extract.get("chars", 0)
            record["text_note"] = extract.get("note", "")
        cleaned.append(record)
    return cleaned
