"""What a conversation's bot has seen of its files, and telling it when they change.

When the bot reads or writes a file with its file tools (read_file, write_file, edit_file, and the
shared folder's), the file's size, time and hash are noted for that conversation, with a copy of
the text when it is small. After each tool call the noted files are looked at again (on the host:
the computer's /workspace and home, and /shared, are folders on this server, so it is a stat, not a
call into the computer). A file that changed is reported with that call's result, as changed by
that call, or, when it changed between calls or turns, as changed outside this conversation (the
user, another conversation, a background job). A small text file's report shows the changed lines;
a bigger one only says that it changed.

write_file on a file that changed since the bot last saw it is refused unless it says
overwrite: it would replace changes it never read. edit_file is not stopped: its exact old_text
already fails on text that moved. A change shown in full counts as seen; one only announced does
not, so the bot reads the file again before overwriting it.

Kept in memory per conversation while the server runs (an API restart starts afresh, and nothing
is refused then). Files elsewhere in the computer (/tmp, /etc) and on other machines are not tracked.
"""

from __future__ import annotations

import difflib
import hashlib
import posixpath
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.services.computer_provider import COMPUTER_HOME, COMPUTER_WORKSPACE, computer_id_for_bot

MAX_FILES = 60  # per conversation, the least recently seen go first
MAX_THREADS = 300
KEEP_TEXT_BYTES = 64 * 1024
HASH_MAX_BYTES = 20 * 1024 * 1024
DIFF_MAX_LINES = 40
DIFF_MAX_CHARS = 4000
SHARED = "/shared"


@dataclass
class Snapshot:
    size: int
    mtime_ns: int
    sha: str
    text: Optional[str]  # small text files only


@dataclass
class Tracked:
    bot_id: str
    host: Path
    now: Snapshot  # as last looked at
    seen_sha: str  # what the bot knows the file to be


_threads: "OrderedDict[str, OrderedDict[str, Tracked]]" = OrderedDict()


def computer_path(path: str) -> str:
    text = (path or "").strip()
    if text == "~" or text.startswith("~/"):
        text = COMPUTER_HOME + text[1:]
    elif not text.startswith("/"):
        text = posixpath.join(COMPUTER_WORKSPACE, text)
    return posixpath.normpath(text)


def shared_path(relative: str) -> str:
    return posixpath.normpath(posixpath.join(SHARED, (relative or "").lstrip("/")))


def host_path(bot_id: str, path: str) -> Optional[Path]:
    """Where a path of the bot's computer is on this server; None when it is not reachable from here."""
    if settings.COMPUTER_PROVIDER != "docker" or settings.COMPUTER_DOCKER_WORKSPACE_MODE != "bind":
        roots = {}
    else:
        computer = settings.COMPUTER_DOCKER_WORKSPACE_ROOT / computer_id_for_bot(bot_id)
        roots = {COMPUTER_WORKSPACE: computer, COMPUTER_HOME: computer.with_name(computer.name + ".home")}
    roots[SHARED] = Path(settings.WORKSPACE_ROOT)
    for prefix, root in roots.items():
        if path == prefix or path.startswith(prefix + "/"):
            target = (root / path[len(prefix):].lstrip("/")).resolve()
            base = root.resolve()
            return target if target == base or base in target.parents else None
    return None


def snapshot(host: Path) -> Optional[Snapshot]:
    try:
        stat = host.stat()
        if not host.is_file() or stat.st_size > HASH_MAX_BYTES:
            return None
        data = host.read_bytes()
    except OSError:
        return None
    text = None
    if len(data) <= KEEP_TEXT_BYTES and b"\0" not in data[:4096]:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
    return Snapshot(stat.st_size, stat.st_mtime_ns, hashlib.sha256(data).hexdigest(), text)


def _files(thread_id: str) -> "OrderedDict[str, Tracked]":
    files = _threads.get(thread_id)
    if files is None:
        files = _threads[thread_id] = OrderedDict()
        while len(_threads) > MAX_THREADS:
            _threads.popitem(last=False)
    _threads.move_to_end(thread_id)
    return files


def note_seen(thread_id: str, bot_id: str, path: str) -> None:
    """The bot read or wrote this file: what it is now is what the bot knows."""
    host = host_path(bot_id, path)
    taken = host and snapshot(host)
    files = _files(thread_id)
    if not taken:
        files.pop(path, None)
        return
    files[path] = Tracked(bot_id, host, taken, taken.sha)
    files.move_to_end(path)
    while len(files) > MAX_FILES:
        files.popitem(last=False)


def _diff(path: str, before: Optional[str], after: Optional[str]) -> Tuple[str, bool]:
    """The changed lines, and whether that is all of the change."""
    if before is None or after is None:
        return "", False
    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(), fromfile=path, tofile=path, n=1, lineterm=""))[2:]
    text = "\n".join(lines)
    if len(lines) > DIFF_MAX_LINES or len(text) > DIFF_MAX_CHARS:
        return "\n".join(lines[:DIFF_MAX_LINES])[:DIFF_MAX_CHARS] + "\n[… more changes]", False
    return text, True


def changes(thread_id: str, *, skip: Tuple[str, ...] = ()) -> List[Dict[str, Any]]:
    """The tracked files that changed since they were last looked at (each reported once)."""
    files = _threads.get(thread_id)
    if not files:
        return []
    found: List[Dict[str, Any]] = []
    for path, tracked in list(files.items()):
        if path in skip:
            continue
        try:
            stat = tracked.host.stat()
        except OSError:
            files.pop(path, None)
            found.append({"path": path, "change": "deleted"})
            continue
        if stat.st_size == tracked.now.size and stat.st_mtime_ns == tracked.now.mtime_ns:
            continue
        taken = snapshot(tracked.host)
        if taken is None:
            files.pop(path, None)
            found.append({"path": path, "change": "changed (it is no longer a text file this can follow)"})
            continue
        if taken.sha == tracked.now.sha:
            tracked.now = taken  # touched, not changed
            continue
        diff, whole = _diff(path, tracked.now.text, taken.text)
        entry: Dict[str, Any] = {"path": path, "change": "changed"}
        if diff:
            entry["diff"] = diff
        if whole and tracked.seen_sha == tracked.now.sha:
            tracked.seen_sha = taken.sha  # shown in full: the bot knows it now
        else:
            entry["note"] = "Read it again before writing it whole."
        tracked.now = taken
        found.append(entry)
    return found


def overwrite_refusal(thread_id: str, bot_id: str, path: str) -> Optional[str]:
    """Why write_file should not replace this file, or None: it changed since the bot last saw it."""
    tracked = (_threads.get(thread_id) or {}).get(path)
    if tracked is None:
        return None
    taken = snapshot(tracked.host)
    if taken is None or taken.sha == tracked.seen_sha:
        return None
    return (f"{path} changed since you last read it, and writing it whole would undo that change. Read it again and write from "
            "what is there now (or use edit_file), or pass overwrite: true to replace it anyway.")


def forget(thread_id: str) -> None:
    _threads.pop(thread_id, None)
