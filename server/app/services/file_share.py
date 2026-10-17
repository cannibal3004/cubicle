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
    raise FileShareError("source must be 'workspace' or 'computer'.")


def resolve_shared_file(source: str, path: str, bot_id: Optional[str]) -> SharedFile:
    """Locate a file inside a shareable root, or raise FileShareError."""
    if not isinstance(path, str) or not path.strip():
        raise FileShareError("A file path is required.")
    if len(path) > 1000:
        raise FileShareError("The file path is too long.")
    candidate = path.strip().replace("\\", "/")
    # The computer sees its workspace as /workspace and the shared folder as
    # /shared; accept those spellings, and treat /shared as what it is.
    if source == "computer":
        if candidate == "/shared" or candidate.startswith("/shared/"):
            source, candidate = "workspace", candidate[len("/shared"):] or "."
        elif candidate == "/workspace":
            candidate = "."
        elif candidate.startswith("/workspace/"):
            candidate = candidate[len("/workspace/"):]
    root = source_root(source, bot_id).resolve()
    absolute = (root / candidate.lstrip("/")).resolve()
    if absolute != root and root not in absolute.parents:
        raise FileShareError("The path is outside the shareable directory.")
    if not absolute.exists():
        raise FileShareError(f"No such file: {path}")
    if not absolute.is_file():
        raise FileShareError("Only files can be shared, not directories.")
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
