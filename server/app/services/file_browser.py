"""Browsing, uploading to and tidying the folders a bot can reach.

Two roots: the bot's computer workspace (its /workspace, which in bind mode
is a directory on this host) and the shared folder. Everything here works
on the host copy, so it needs no running computer and nothing goes through
the model: a file dropped in here is simply there the next time the bot
looks, with no attachment card and no extracted text.
"""

from __future__ import annotations

import mimetypes
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.config import settings
from app.services.file_share import SOURCES, FileShareError, normalize_source, private_message, source_root

MAX_ENTRIES = 2000
MAX_NAME_LENGTH = 180


def _root(source: str, bot_id: Optional[str]) -> Tuple[str, Path]:
    source = normalize_source(source)
    if source not in SOURCES:
        raise FileShareError("source must be 'shared' or 'computer'.")
    return source, source_root(source, bot_id).resolve()


def _confine(root: Path, path: str) -> Path:
    """The absolute path for `path` inside `root`, or an error if it would leave it."""
    candidate = (path or "").strip().replace("\\", "/")
    if candidate in ("/workspace", "/shared"):
        candidate = ""
    for prefix in ("/workspace/", "/shared/"):
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix):]
    if len(candidate) > 1000:
        raise FileShareError("The path is too long.")
    # resolve() follows symlinks, so a link pointing outside the root is refused too.
    absolute = (root / candidate.lstrip("/")).resolve()
    if absolute != root and root not in absolute.parents:
        raise FileShareError("The path is outside the browsable directory.")
    return absolute


def _relative(root: Path, absolute: Path) -> str:
    relative = absolute.relative_to(root).as_posix()
    return "" if relative == "." else relative


def _entry(root: Path, child: Path) -> Optional[Dict[str, Any]]:
    try:
        stat = child.stat()
        is_dir = child.is_dir()
    except OSError:
        return None
    return {
        "name": child.name,
        "path": _relative(root, child),
        "kind": "dir" if is_dir else "file",
        "size": None if is_dir else stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "mime": None if is_dir else (mimetypes.guess_type(child.name)[0] or "application/octet-stream"),
        # Kept to itself by the computer's own user (a .ssh folder, a private key): not readable from here.
        "private": not os.access(child, os.R_OK | os.X_OK if is_dir else os.R_OK),
    }


def list_directory(source: str, path: str, bot_id: Optional[str]) -> Dict[str, Any]:
    """The entries of one folder, directories first; a folder that does not exist yet lists as empty."""
    source, root = _root(source, bot_id)
    folder = _confine(root, path)
    relative = _relative(root, folder)
    parent = None if not relative else (relative.rsplit("/", 1)[0] if "/" in relative else "")
    result: Dict[str, Any] = {"source": source, "path": relative, "parent": parent, "entries": [], "exists": folder.exists()}
    try:
        if not folder.exists():
            return result
        if not folder.is_dir():
            raise FileShareError("That is a file, not a folder.")
        children = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        raise FileShareError(private_message(folder.name or "This folder")) from None
    for child in children[:MAX_ENTRIES]:
        entry = _entry(root, child)
        if entry is not None:
            result["entries"].append(entry)
    result["truncated"] = len(children) > MAX_ENTRIES
    return result


def safe_name(name: str) -> str:
    """A plain file name: no directories or control characters, bounded, suffix kept."""
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


def _open_to_the_computer(path: Path, mode: int) -> None:
    # The computer runs as a different unprivileged user than the API; what
    # the API puts into a bind-mounted workspace must be open to it.
    try:
        path.chmod(mode)
    except OSError:
        pass


def _unique(folder: Path, name: str) -> Path:
    target = folder / name
    stem, dot, suffix = name.rpartition(".")
    if not dot or not stem:
        stem, suffix = name, ""
    counter = 2
    while target.exists():
        target = folder / (f"{stem} ({counter}).{suffix}" if suffix else f"{stem} ({counter})")
        counter += 1
    return target


def store_files(source: str, path: str, bot_id: Optional[str], files: Iterable[Tuple[str, bytes]]) -> List[Dict[str, Any]]:
    """Write uploaded files into a folder (created if needed); a clash gets a numbered name."""
    source, root = _root(source, bot_id)
    folder = _confine(root, path)
    if folder.exists() and not folder.is_dir():
        raise FileShareError("That is a file, not a folder.")
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FileShareError(f"Could not create the folder: {exc}") from exc
    _open_to_the_computer(root, 0o777)
    current = folder
    while current != root:
        _open_to_the_computer(current, 0o777)
        current = current.parent
    stored: List[Dict[str, Any]] = []
    for name, data in files:
        if not data:
            raise FileShareError(f"{name or 'The file'} is empty.")
        if len(data) > settings.ATTACHMENT_MAX_BYTES:
            raise FileShareError(f"{name} is larger than the {settings.ATTACHMENT_MAX_BYTES // (1024 * 1024)} MB upload limit.")
        target = _unique(folder, safe_name(name))
        target.write_bytes(data)
        _open_to_the_computer(target, 0o666)
        entry = _entry(root, target)
        if entry is not None:
            stored.append(entry)
    return stored


def ensure_directory(source: str, path: str, bot_id: Optional[str]) -> None:
    """Create the folder at `path` (and its parents) unless it is there already."""
    _, root = _root(source, bot_id)
    folder = root
    try:
        for part in _confine(root, path).relative_to(root).parts:
            folder = folder / part
            if not folder.exists():
                folder.mkdir()
                _open_to_the_computer(folder, 0o777)
            elif not folder.is_dir():
                raise FileShareError(f"{part} is a file, not a folder.")
    except OSError as exc:
        raise FileShareError(f"Could not create the folder: {exc}") from exc


def make_directory(source: str, path: str, bot_id: Optional[str], name: str) -> Dict[str, Any]:
    source, root = _root(source, bot_id)
    folder = _confine(root, path)
    clean = safe_name(name)
    target = _confine(root, f"{_relative(root, folder)}/{clean}" if _relative(root, folder) else clean)
    if target.exists():
        raise FileShareError("Something with that name already exists.")
    try:
        target.mkdir(parents=True)
    except OSError as exc:
        raise FileShareError(f"Could not create the folder: {exc}") from exc
    _open_to_the_computer(target, 0o777)
    return _entry(root, target) or {}


def delete_entry(source: str, path: str, bot_id: Optional[str]) -> Dict[str, Any]:
    """Remove one file, or one empty folder. The root itself cannot be removed."""
    source, root = _root(source, bot_id)
    target = _confine(root, path)
    if target == root:
        raise FileShareError("The root folder cannot be deleted.")
    if not target.exists():
        raise FileShareError("No such file or folder.")
    try:
        if target.is_dir():
            if any(target.iterdir()):
                raise FileShareError("The folder is not empty; delete what is inside it first.")
            target.rmdir()
        else:
            target.unlink()
    except OSError as exc:
        raise FileShareError(f"Could not delete it: {exc}") from exc
    return {"deleted": _relative(root, target)}
