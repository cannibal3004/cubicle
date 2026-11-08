"""Files a user attaches to a message.

An upload is saved where the bot can reach it with its ordinary tools: the
bot's computer workspace (its /workspace, in bind mode) or else the shared
folder. The chat shows it as the same download card a bot's share_file
produces, and the model gets the file's text, converted from PDF and Office
formats with MarkItDown and capped so one big document cannot fill the
context; above the cap the model is told where the whole file is.

A PDF whose pages are pictures (a scan) is read with OCR instead
(services/ocr.py): for a while as the message is sent, the remaining pages in
the background. What was read is saved next to the PDF as <name>.ocr.md, and
when the last page is done the conversation gets a message saying so (like a
background job's). A background read is lost if the API restarts.
"""

import asyncio
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple
import uuid
from datetime import datetime

import logging

from app.config import settings
from app.services import ocr, retrieval
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
    ocr: str = ""  # for a scan: what the model should know about text read with OCR


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
    """The file's text, capped at `limit` characters (a scan's pages all read).
    Never raises: a file that cannot be read as text comes back with an empty
    text and a note."""
    return _extract(path, limit)[0]


def _scan_note(scan: "ocr.Scan") -> str:
    note = "read from a scan with OCR"
    if len(scan.scanned) + len(scan.skipped) < len(scan.texts):
        note = "the scanned pages read with OCR, the others from the PDF's own text"
    return note + (" (not all pages yet)" if scan.pending else "")


def _extract(path: Path, limit: Optional[int] = None, ocr_seconds: Optional[float] = None) -> Tuple[Extract, Optional["ocr.Scan"]]:
    """extract_text, and for a scan what was read of it; OCR stops starting
    new pages after `ocr_seconds` (the rest are the Scan's pending pages)."""
    cap = settings.ATTACHMENT_INLINE_MAX_CHARS if limit is None else limit
    suffix = path.suffix.lower()
    text: Optional[str] = None
    note = ""
    if suffix == ".pdf":
        scan = ocr.open_scan(path)
        if scan is not None:
            scan.read(None if ocr_seconds is None else time.monotonic() + ocr_seconds)
            if scan.found_text or scan.pending:  # else only blank pages: the PDF's own text is all there is
                return _capped(scan.text(), cap, _scan_note(scan), OCR_CAVEAT.format(pages=ocr.page_list(scan.scanned))), scan
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
        return Extract(text="", chars=0, truncated=False, note=note), None
    return _capped(text, cap, note), None


def _capped(text: str, cap: int, note: str, ocr_note: str = "") -> Extract:
    text = text.strip()
    total = len(text)
    truncated = total > cap
    if truncated:
        text = text[:cap]
    return Extract(text=text, chars=total, truncated=truncated, note=note, ocr=ocr_note)


OCR_CAVEAT = (
    "A scan, read with OCR ({pages}). OCR misreads characters, most often digits and symbols ($ read as 5, 0 as O): "
    "before relying on a figure, check it on the page itself (open the PDF in your computer's browser and take a "
    "screenshot) or ask the user."
)
SCAN_SUFFIX = ".ocr.md"

# Background reads of scans, kept referenced until they end.
_scan_tasks: Set["asyncio.Task[None]"] = set()


async def attach(shared: SharedFile, bot_id: Optional[str], thread_id: Optional[str] = None) -> Dict[str, Any]:
    """The attachment record for a message: card fields, location and the
    text for the model (extracted in a worker thread). A scan's pages not read
    in time go on in the background, and `thread_id` hears when they are done."""
    record = describe(shared, bot_id)
    # Extracted once in full: the message inlines the first part, and the
    # whole text is indexed for retrieval when embeddings are available.
    scan = None
    try:
        full, scan = await asyncio.wait_for(
            asyncio.to_thread(_extract, shared.absolute, retrieval.MAX_INDEX_CHARS, settings.OCR_INLINE_SECONDS),
            timeout=settings.ATTACHMENT_EXTRACT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        full = Extract(text="", chars=0, truncated=False, note=f"text extraction did not finish within {settings.ATTACHMENT_EXTRACT_TIMEOUT:.0f}s")
    except Exception as exc:  # pragma: no cover - extract_text should not raise
        full = Extract(text="", chars=0, truncated=False, note=f"no text could be extracted ({type(exc).__name__})")
    cap = settings.ATTACHMENT_INLINE_MAX_CHARS
    extract = Extract(text=full.text[:cap], chars=full.chars, truncated=full.chars > cap, note=full.note, ocr=full.ocr)
    if scan is not None:
        saved = save_scan_text(shared, scan)
        extract = replace(extract, ocr=extract.ocr + _scan_progress(scan, shared, saved))
        if scan.pending:
            _finish_scan_later(scan, shared, record, bot_id, thread_id, saved)
    record["extract"] = asdict(extract)
    if full.text and bot_id and (scan is None or not scan.pending):
        schedule_indexing(bot_id, record, full.text)
    return record


# ---- scans ----------------------------------------------------------------------

def _scan_file(shared: SharedFile) -> Tuple[Path, str]:
    """Where a scan's text is saved: on disk, and as the bot finds it."""
    return shared.absolute.with_name(shared.absolute.name + SCAN_SUFFIX), location_for(shared.source, shared.path + SCAN_SUFFIX)


def save_scan_text(shared: SharedFile, scan: "ocr.Scan") -> Optional[str]:
    """Write what was read of the scan next to it; where the bot finds it, or None when it could not be written."""
    target, location = _scan_file(shared)
    lines = [f"# {shared.name}, read with OCR", "", "Pages that are pictures (a scan) were read with OCR: characters can be misread, digits and symbols most often.", ""]
    if scan.pending:
        lines += [f"(Still being read: {ocr.page_list(scan.pending)}; each is added here as it is done.)", ""]
    if scan.skipped:
        lines += [f"(Not read: {ocr.page_list(scan.skipped)}; only the first {len(scan.scanned)} scanned pages are.)", ""]
    try:
        target.write_text("\n".join(lines) + "\n" + scan.text() + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not save the OCR text of %s: %s", shared.name, exc)
        return None
    _world_writable(target, 0o666)
    return location


def _scan_progress(scan: "ocr.Scan", shared: SharedFile, saved: Optional[str]) -> str:
    parts = []
    if scan.pending:
        where = f" each is added to {saved} as it is done, and" if saved else ""
        parts.append(f"Still being read: {ocr.page_list(scan.pending)};{where} a message comes to this conversation when all are.")
    elif saved:
        parts.append(f"The text read is also saved at {saved}.")
    if scan.skipped:
        parts.append(f"Not read: {ocr.page_list(scan.skipped)} (only the first {len(scan.scanned)} scanned pages are).")
    return (" " + " ".join(parts)) if parts else ""


def _finish_scan_later(scan: "ocr.Scan", shared: SharedFile, record: Dict[str, Any], bot_id: Optional[str], thread_id: Optional[str], saved: Optional[str]) -> None:
    """Read the scan's remaining pages in the background, then index the whole text and tell the conversation."""

    async def run() -> None:
        try:
            await asyncio.to_thread(scan.read, None, lambda: save_scan_text(shared, scan))
        except Exception as exc:  # pragma: no cover - read_page does not raise
            logger.warning("Reading the rest of %s failed: %s", shared.name, exc)
        save_scan_text(shared, scan)
        text = scan.text()
        if text and bot_id:
            schedule_indexing(bot_id, record, text)
        if thread_id and bot_id:
            await _tell_scan_read(thread_id, bot_id, shared, scan, len(text), saved)

    try:
        # The task keeps this request's owner and actor (it copies the context).
        task = asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        return
    _scan_tasks.add(task)
    task.add_done_callback(_scan_tasks.discard)


SCAN_TELL_WAIT_SECONDS = 6 * 3600


async def _tell_scan_read(thread_id: str, bot_id: str, shared: SharedFile, scan: "ocr.Scan", chars: int, saved: Optional[str]) -> None:
    """Post that the scan is read, once no reply is running in the conversation."""
    from app.routers import chat as chat_router  # the turn machinery; imported here to avoid a cycle
    from app.services import conversations
    from app.services.turn_manager import TurnBusyError

    where = f" All of it is in {saved}." if saved else " Its passages are looked up for you when a message asks about them."
    text = (f"[The scanned file {shared.name} has been read to the end.] It was read with OCR ({ocr.page_list(scan.scanned)}, "
            f"{chars:,} characters of text in all).{where} Carry on with what you were doing, or tell the user what it says.")
    message = {
        "id": f"msg-{uuid.uuid4().hex[:6]}",
        "bot_id": bot_id,
        "sender": "user",
        "text": text,
        "created_at": datetime.now().isoformat(),
        "item_type": "user_text",
        "raw_payload": {"scan_read": {"name": shared.name, "pages": len(scan.scanned)}},
    }
    owner = get_current_owner()
    give_up = time.monotonic() + SCAN_TELL_WAIT_SECONDS
    while time.monotonic() < give_up:
        if conversations.get(thread_id) is None and thread_id != bot_id:
            return  # the conversation was deleted
        try:
            await chat_router.start_scheduled_turn(thread_id, message, owner)
            return
        except TurnBusyError:
            await asyncio.sleep(5)  # told after the reply in progress ends
        except Exception as exc:
            logger.warning("Could not tell %s that %s was read: %s", thread_id, shared.name, exc)
            return


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
    if extract.get("ocr"):
        header += f"\n({extract['ocr']})"
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
