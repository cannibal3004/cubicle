"""Files a bot can hand to the user as a download card in the chat.

Two roots are shareable: the shared folder (the host directory the user
chose, which a computer sees at /shared), and the bot's own computer
workspace (its /workspace, which in bind mode is a directory under the API's
data directory). A share never copies or moves
anything; it records a pointer the chat can download later, after the same
confinement checks.
"""

import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from app.config import settings
from app.services.action_gateway import ActionDefinition, ActionInvocation, action_gateway
from app.services.computer_provider import computer_id_for_bot

SOURCES = ("workspace", "computer")
# "workspace" is the stored name of the shared folder (it predates the
# computers, whose own directory is also called /workspace); the tool and
# the prompt say "shared", so both spellings are accepted.
SOURCE_ALIASES = {"shared": "workspace", "shared_folder": "workspace", "workspace": "workspace", "computer": "computer"}


def normalize_source(source: Any) -> str:
    return SOURCE_ALIASES.get(str(source or "").strip().lower(), str(source or ""))

# Slim images ship a short MIME table; make sure the common document types
# are named on cards rather than falling back to octet-stream.
for _mime, _suffix in (
    ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    ("application/msword", ".doc"),
    ("application/vnd.ms-excel", ".xls"),
    ("application/vnd.ms-powerpoint", ".ppt"),
    ("text/markdown", ".md"),
    ("text/csv", ".csv"),
    ("application/json", ".json"),
    ("application/yaml", ".yaml"),
):
    mimetypes.add_type(_mime, _suffix)
SHARE_ACTION = "files.share"


class FileShareError(ValueError):
    """Raised when a path cannot be shared or served."""


@dataclass(frozen=True)
class SharedFile:
    source: str
    path: str  # relative to the source root, forward slashes
    name: str
    size: int
    mime: str
    absolute: Path

    def to_attachment(self) -> Dict[str, Any]:
        return {"source": self.source, "path": self.path, "name": self.name, "size": self.size, "mime": self.mime}


def source_root(source: str, bot_id: Optional[str]) -> Path:
    if source == "workspace":
        return settings.WORKSPACE_ROOT
    if source == "computer":
        if not bot_id:
            raise FileShareError("Sharing from the computer needs a bot id.")
        if settings.COMPUTER_PROVIDER != "docker":
            raise FileShareError("The computer is not available with this computer provider.")
        if settings.COMPUTER_DOCKER_WORKSPACE_MODE != "bind":
            raise FileShareError(
                "Files on the computer can only be shared when COMPUTER_DOCKER_WORKSPACE_MODE=bind; "
                "copy the file to the shared folder instead."
            )
        return settings.COMPUTER_DOCKER_WORKSPACE_ROOT / computer_id_for_bot(bot_id)
    raise FileShareError("source must be 'shared' or 'computer'.")


def _locate(source: str, candidate: str, bot_id: Optional[str]) -> tuple:
    root = source_root(source, bot_id).resolve()
    absolute = (root / candidate.lstrip("/")).resolve()
    if absolute != root and root not in absolute.parents:
        raise FileShareError("The path is outside the shareable directory.")
    return root, absolute


def private_message(name: str) -> str:
    """For a file or folder the computer's own user keeps to itself (a .ssh folder, a private key):
    the API, which runs as another user, cannot read it, and should not."""
    return (
        f"{name} is private to the computer's own user (as a .ssh folder or a private key should be), so it cannot be "
        "opened here; the bot can still use it in its computer."
    )


def resolve_shared_file(source: str, path: str, bot_id: Optional[str]) -> SharedFile:
    """Locate a file inside a shareable root, or raise FileShareError.

    The two roots are easy to mix up (the shared folder is stored as
    "workspace"; the computer's own directory is /workspace), so the path's
    spelling wins over the source, and a file missing from the chosen root
    is looked for in the other one before giving up.
    """
    if not isinstance(path, str) or not path.strip():
        raise FileShareError("A file path is required.")
    if len(path) > 1000:
        raise FileShareError("The file path is too long.")
    source = normalize_source(source)
    candidate = path.strip().replace("\\", "/")
    # The computer sees its workspace as /workspace and the shared folder as
    # /shared; those spellings say where the file is whatever the source.
    if candidate == "/shared" or candidate.startswith("/shared/"):
        source, candidate = "workspace", candidate[len("/shared"):] or "."
    elif candidate == "/workspace":
        source, candidate = "computer", "."
    elif candidate.startswith("/workspace/"):
        source, candidate = "computer", candidate[len("/workspace/"):]
    try:
        return _resolve_located(source, candidate, path, bot_id)
    except PermissionError:
        raise FileShareError(private_message(Path(candidate).name or path)) from None


def _resolve_located(source: str, candidate: str, path: str, bot_id: Optional[str]) -> SharedFile:
    root, absolute = _locate(source, candidate, bot_id)
    if not absolute.exists():
        other = "computer" if source == "workspace" else "workspace"
        try:
            other_root, other_absolute = _locate(other, candidate, bot_id)
        except FileShareError:
            other_absolute = None
        if other_absolute is not None and other_absolute.is_file():
            source, root, absolute = other, other_root, other_absolute
        else:
            looked = "the shared folder and the computer's /workspace" if other_absolute is not None else (
                "the shared folder" if source == "workspace" else "the computer's /workspace"
            )
            raise FileShareError(f"No such file: {path} (looked in {looked}).")
    if not absolute.is_file():
        raise FileShareError("Only files can be shared, not directories.")
    if not os.access(absolute, os.R_OK):
        raise FileShareError(private_message(absolute.name))
    size = absolute.stat().st_size
    if size > settings.FILE_SHARE_MAX_BYTES:
        raise FileShareError(f"The file is larger than the {settings.FILE_SHARE_MAX_BYTES}-byte sharing limit.")
    mime = mimetypes.guess_type(absolute.name)[0] or "application/octet-stream"
    relative = absolute.relative_to(root).as_posix()
    return SharedFile(source=source, path=relative, name=absolute.name, size=size, mime=mime, absolute=absolute)


async def execute_share(call: ActionInvocation) -> Dict[str, Any]:
    source = str(call.arguments.get("source") or "")
    path = str(call.arguments.get("path") or "")
    bot_id = call.target.get("bot_id") if isinstance(call.target, dict) else None
    try:
        shared = resolve_shared_file(source, path, bot_id)
    except FileShareError as exc:
        raise FileShareError(str(exc)) from exc
    return {
        "shared": True,
        "attachment": shared.to_attachment(),
        "note": "The user now has a download card for this file in the chat; refer to it by name.",
    }


action_gateway.register_action(
    ActionDefinition(
        name=SHARE_ACTION,
        tool="files",
        action="share",
        intent="Offer a file from the shared folder or the computer to the user as a download.",
        risk="read",
        requires_approval=False,
    ),
    execute_share,
)
