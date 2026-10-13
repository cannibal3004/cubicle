import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from app.config import settings


class WorkspaceToolError(ValueError):
    """Raised when a workspace request is invalid or outside the workspace root."""


@dataclass(frozen=True)
class WorkspaceToolCall:
    name: str
    path: str
    content: Optional[str] = None

    @property
    def arguments_for_display(self) -> Dict[str, Any]:
        args: Dict[str, Any] = {"path": self.path}
        if self.content is not None:
            args["bytes"] = len(self.content.encode("utf-8"))
        return args

    @property
    def summary(self) -> str:
        if self.name == "workspace.read":
            return f"Read from the shared folder: {self.path}"
        if self.name == "workspace.write":
            return f"Write {len(self.content or '')} characters to the shared folder: {self.path}"
        if self.name == "workspace.delete":
            return f"Delete from the shared folder: {self.path}"
        return f"List the shared folder: {self.path}"


# The slash command; the gateway actions keep their older "workspace" name.
COMMAND = "/shared"
ACTIONS = ("list", "read", "write", "delete")


def parse_workspace_command(prompt: str) -> Optional[WorkspaceToolCall]:
    """Parse an explicit, non-shell shared folder command from a user turn.

    Supported forms:
      /shared list [path]
      /shared read <path>
      /shared write <path>\n<content>
      /shared delete <path>
    """
    text = (prompt or "").strip()
    if not text.lower().startswith(COMMAND):
        return None

    lines = text.split("\n", 1)
    try:
        header = shlex.split(lines[0])
    except ValueError as exc:
        raise WorkspaceToolError(f"Invalid {COMMAND} command: {exc}") from exc

    if len(header) < 2 or header[0].lower() != COMMAND:
        raise WorkspaceToolError(f"Use {COMMAND} list, read, write or delete.")

    action = header[1].lower()
    if action not in ACTIONS:
        raise WorkspaceToolError("Supported shared folder actions are list, read, write and delete.")

    if action == "write":
        if len(header) != 3 or len(lines) != 2 or not lines[1]:
            raise WorkspaceToolError(
                f"Write syntax: {COMMAND} write <path> followed by file content on the next line."
            )
        return WorkspaceToolCall("workspace.write", header[2], lines[1])

    if len(header) > 3:
        raise WorkspaceToolError(f"{action.title()} accepts one path argument.")
    if action in ("read", "delete") and len(header) != 3:
        raise WorkspaceToolError(f"{action.title()} needs a path.")

    path = header[2] if len(header) == 3 else "."
    return WorkspaceToolCall(f"workspace.{action}", path)


def _loosen(path: Path, mode: int) -> None:
    """Let the sandbox user (a different uid) change what the API created.

    The folder is shared with every bot's computer, which runs as its own
    unprivileged user; the computers do the same for what they create.
    """
    try:
        os.chmod(path, mode)
    except OSError:
        pass  # Windows, or a filesystem without modes: not fatal.


class WorkspaceService:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _resolve(self, user_path: str, must_exist: bool = False) -> Path:
        if not user_path or "\x00" in user_path:
            raise WorkspaceToolError("A workspace-relative path is required.")

        candidate = Path(user_path).expanduser()
        if candidate.is_absolute():
            raise WorkspaceToolError("Absolute paths are not allowed.")

        resolved = (self.root / candidate).resolve(strict=False)
        if resolved != self.root and self.root not in resolved.parents:
            raise WorkspaceToolError("That path is outside the configured workspace.")
        if must_exist and not resolved.exists():
            raise WorkspaceToolError(f"Workspace path does not exist: {user_path}")
        if resolved == self.root:
            return resolved
        return resolved

    def existing_size(self, user_path: str) -> Optional[int]:
        """Size of an existing workspace file, or None if there is none (or the path is invalid)."""
        try:
            path = self._resolve(user_path)
        except WorkspaceToolError:
            return None
        try:
            return path.stat().st_size if path.is_file() else None
        except OSError:
            return None

    def execute(self, call: WorkspaceToolCall) -> Dict[str, Any]:
        if call.name == "workspace.list":
            return self._list(call.path)
        if call.name == "workspace.read":
            return self._read(call.path)
        if call.name == "workspace.write":
            return self._write(call.path, call.content or "")
        if call.name == "workspace.delete":
            return self._delete(call.path)
        raise WorkspaceToolError(f"Unknown shared folder tool: {call.name}")

    def _list(self, user_path: str) -> Dict[str, Any]:
        directory = self._resolve(user_path, must_exist=True)
        if not directory.is_dir():
            raise WorkspaceToolError(f"Not a directory: {user_path}")

        entries = []
        for child in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
            safe_child = self._resolve(str(child.relative_to(self.root)))
            kind = "dir" if safe_child.is_dir() else "file"
            entries.append({"name": safe_child.name, "kind": kind})
        return {"path": user_path, "entries": entries}

    def _read(self, user_path: str) -> Dict[str, Any]:
        path = self._resolve(user_path, must_exist=True)
        if not path.is_file():
            raise WorkspaceToolError(f"Not a file: {user_path}")
        size = path.stat().st_size
        if size > settings.WORKSPACE_MAX_FILE_BYTES:
            raise WorkspaceToolError(
                f"File is larger than the {settings.WORKSPACE_MAX_FILE_BYTES}-byte limit."
            )
        return {"path": user_path, "content": path.read_text(encoding="utf-8"), "bytes": size}

    def _write(self, user_path: str, content: str) -> Dict[str, Any]:
        path = self._resolve(user_path)
        if path == self.root:
            raise WorkspaceToolError("A file path is required for workspace.write.")
        if path.exists() and path.is_dir():
            raise WorkspaceToolError(f"Cannot write to a directory: {user_path}")

        encoded = content.encode("utf-8")
        if len(encoded) > settings.WORKSPACE_MAX_FILE_BYTES:
            raise WorkspaceToolError(
                f"Content is larger than the {settings.WORKSPACE_MAX_FILE_BYTES}-byte limit."
            )
        if path.parent.exists() and not path.parent.is_dir():
            raise WorkspaceToolError(f"The destination's parent is a file, not a directory: {user_path}")
        # _resolve already confined the path to the workspace root, and the
        # user approved this exact path, so missing folders can be created.
        missing = []
        parent = path.parent
        while not parent.exists() and parent != self.root:
            missing.append(parent)
            parent = parent.parent
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            for directory in missing:
                _loosen(directory, 0o777)
            # Written beside the target and moved over it: a file the sandbox
            # user made can be replaced this way even when it is not writable
            # by the API, and a reader never sees a half-written file.
            staging = path.with_name(f".{path.name}.{os.getpid()}.part")
            staging.write_text(content, encoding="utf-8")
            _loosen(staging, 0o666)
            os.replace(staging, path)
        except OSError as exc:
            raise WorkspaceToolError(f"Could not write {user_path}: {exc.strerror or exc}") from exc
        return {"path": user_path, "bytes": len(encoded), "written": True, "created_directories": bool(missing)}

    def _delete(self, user_path: str) -> Dict[str, Any]:
        path = self._resolve(user_path, must_exist=True)
        if path == self.root:
            raise WorkspaceToolError("The shared folder itself cannot be deleted.")
        try:
            if path.is_dir() and not path.is_symlink():
                entries = sum(len(files) + len(dirs) for _, dirs, files in os.walk(path))
                shutil.rmtree(path)
                return {"path": user_path, "deleted": True, "kind": "directory", "entries": entries}
            path.unlink()
        except OSError as exc:
            raise WorkspaceToolError(f"Could not delete {user_path}: {exc.strerror or exc}") from exc
        return {"path": user_path, "deleted": True, "kind": "file"}


workspace_service = WorkspaceService(settings.WORKSPACE_ROOT)
