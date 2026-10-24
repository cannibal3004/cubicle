"""Provider-neutral computer runtime contract and deterministic local adapter.

The application talks to a computer through this contract rather than through
vendor-specific browser, desktop, or container APIs. The fake adapter keeps
the lifecycle and action shapes usable in local development and tests without
launching processes; the optional Docker adapter supplies the isolated runtime.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import posixpath
import re
from typing import Any, Dict, List, Literal, Optional, Protocol, Tuple
from urllib.parse import urlparse


ComputerState = Literal["stopped", "starting", "running", "paused", "resetting", "error"]
ComputerHealth = Literal["unknown", "healthy", "unhealthy"]

COMPUTER_CAPABILITIES: Tuple[str, ...] = (
    "browser.navigate",
    "terminal.exec",
    "files.list",
    "screenshot",
    "input",
    "cleanup",
    # Who holds the mouse and keyboard, and the bot asking for a human.
    "control",
    "takeover",
)

ControlOwner = Literal["bot", "user"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _computer_id(bot_id: str) -> str:
    if not isinstance(bot_id, str) or not bot_id.strip():
        raise ComputerProviderError("A bot id is required to create a computer.")
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", bot_id.strip()).strip("-")
    if not normalized:
        raise ComputerProviderError("The bot id cannot produce a valid computer id.")
    return f"computer-{normalized[:80]}"


def computer_id_for_bot(bot_id: str) -> str:
    """Return the stable provider id without creating a runtime."""

    return _computer_id(bot_id)


@dataclass
class ComputerStatus:
    computer_id: str
    bot_id: str
    provider: str
    state: ComputerState
    health: ComputerHealth
    width: int
    height: int
    fps: int
    generation: int
    capabilities: Tuple[str, ...]
    updated_at: str
    frame_id: Optional[str] = None
    url: Optional[str] = None
    last_operation: Optional[str] = None
    # "bot" by default. While "user", the bot's pointer/keyboard/navigation
    # actions are refused so the two do not fight over the same desktop.
    controlled_by: str = "bot"
    # Set when the bot asked the user to take over; cleared when control
    # returns to the bot.
    takeover_request: Optional[Dict[str, Any]] = None
    # Ubuntu packages this computer installed; restored at every start.
    packages: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["capabilities"] = list(self.capabilities)
        return value


# A shell command is one argument to `sh -lc`; Linux allows 128 KiB per
# argument, so this leaves room for multi-byte text. File tools exist so that
# source files do not have to travel through the shell at all.
TERMINAL_COMMAND_MAX_CHARS = 60000
FILE_CONTENT_MAX_CHARS = 200_000
FILE_PATH_MAX_CHARS = 512
# Ubuntu package names: lower-case letters, digits, and the few symbols
# dpkg allows (plus, dot, colon for an architecture, tilde, hyphen). No
# leading hyphen, so a name can never read as an apt option.
PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.:~-]{0,99}$")
MAX_PACKAGES_PER_CALL = 50


COMPUTER_WORKSPACE = "/workspace"
COMPUTER_HOME = "/home/pwuser"


def check_file_path(path: Any) -> str:
    """An absolute sandbox path: a relative one is taken from /workspace, ~ is the home directory."""
    if not isinstance(path, str) or not path.strip() or len(path) > FILE_PATH_MAX_CHARS:
        raise ComputerProviderError(f"A file path is required and must be at most {FILE_PATH_MAX_CHARS} characters.")
    text = path.strip()
    if text == "~" or text.startswith("~/"):
        text = COMPUTER_HOME + text[1:]
    elif not text.startswith("/"):
        text = posixpath.join(COMPUTER_WORKSPACE, text)
    return posixpath.normpath(text)


def check_command(command: Any) -> str:
    if not isinstance(command, str) or not command.strip():
        raise ComputerProviderError("A terminal command is required.")
    if len(command) > TERMINAL_COMMAND_MAX_CHARS:
        raise ComputerProviderError(f"Terminal commands must be at most {TERMINAL_COMMAND_MAX_CHARS} characters.")
    return command


def check_timeout(value: Any) -> float:
    """Seconds a terminal command may run: the model's request, clamped; the default when absent."""
    from app.config import settings  # imported here, as the provider factory does, to keep config import-free

    default = float(settings.COMPUTER_DOCKER_COMMAND_TIMEOUT)
    maximum = max(default, float(settings.COMPUTER_DOCKER_COMMAND_TIMEOUT_MAX))
    if value is None or value == "":
        return default
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ComputerProviderError("'timeout_seconds' must be a number of seconds.") from exc
    if seconds != seconds or seconds < 1:
        raise ComputerProviderError("'timeout_seconds' must be at least 1.")
    return min(seconds, maximum)


def check_file_content(content: Any) -> str:
    if not isinstance(content, str):
        raise ComputerProviderError("File content must be text.")
    if len(content) > FILE_CONTENT_MAX_CHARS:
        raise ComputerProviderError(f"File content must be at most {FILE_CONTENT_MAX_CHARS} characters.")
    return content


def check_package_names(packages: Any) -> List[str]:
    """The cleaned, de-duplicated package list, or an error naming the first bad entry.

    Every route to install_packages runs this before anything else happens,
    so a bad name is refused before an approval is opened rather than after
    the user has said yes.
    """
    if isinstance(packages, str):
        packages = packages.split()
    if not isinstance(packages, (list, tuple)) or not packages:
        raise ComputerProviderError("A list of package names is required.")
    if len(packages) > MAX_PACKAGES_PER_CALL:
        raise ComputerProviderError(f"At most {MAX_PACKAGES_PER_CALL} packages can be installed in one call.")
    names: List[str] = []
    for name in packages:
        cleaned = name.strip().lower() if isinstance(name, str) else ""
        if not PACKAGE_NAME.match(cleaned):
            raise ComputerProviderError(f"Invalid package name: {str(name)[:60]!r}.")
        if cleaned not in names:
            names.append(cleaned)
    return names


class ComputerProviderError(RuntimeError):
    """Raised when a computer adapter cannot satisfy a requested operation."""


class ComputerProvider(Protocol):
    """The contract a real browser/desktop provider must implement."""

    def describe(self, bot_id: str) -> ComputerStatus:
        ...

    def get_or_create(self, bot_id: str) -> ComputerStatus:
        ...

    async def create(self, bot_id: str) -> ComputerStatus:
        ...

    async def start(self, computer_id: str) -> ComputerStatus:
        ...

    async def stop(self, computer_id: str) -> ComputerStatus:
        ...

    async def pause(self, computer_id: str) -> ComputerStatus:
        ...

    async def reset(self, computer_id: str) -> ComputerStatus:
        ...

    async def health(self, computer_id: str) -> ComputerStatus:
        ...

    async def browser_navigate(self, computer_id: str, url: str) -> Dict[str, Any]:
        ...

    async def terminal_execute(self, computer_id: str, command: str, timeout_seconds: Any = None, background: bool = False) -> Dict[str, Any]:
        ...

    async def files_list(self, computer_id: str, path: str = "/workspace") -> Dict[str, Any]:
        ...

    async def files_read(self, computer_id: str, path: str) -> Dict[str, Any]:
        ...

    async def files_write(self, computer_id: str, path: str, content: str) -> Dict[str, Any]:
        ...

    async def files_edit(self, computer_id: str, path: str, old_text: str, new_text: str) -> Dict[str, Any]:
        ...

    async def screenshot(self, computer_id: str) -> Dict[str, Any]:
        ...

    async def send_input(self, computer_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        ...

    async def cleanup(self, computer_id: str) -> Dict[str, Any]:
        ...

    async def set_control(self, computer_id: str, owner: str) -> ComputerStatus:
        ...

    async def request_takeover(self, computer_id: str, reason: str) -> Dict[str, Any]:
        ...

    async def install_packages(self, computer_id: str, packages: Any) -> Dict[str, Any]:
        ...

    async def record_start(self, computer_id: str) -> Dict[str, Any]:
        ...

    async def record_stop(self, computer_id: str) -> Dict[str, Any]:
        ...

    async def record_status(self, computer_id: str) -> Dict[str, Any]:
        ...

    def vnc_target(self, computer_id: str) -> Optional[Dict[str, Any]]:
        """Where the API can bridge a live VNC session, or None if unavailable."""
        ...


def validate_control_owner(owner: Any) -> str:
    value = str(owner or "").strip().lower()
    if value not in {"bot", "user"}:
        raise ComputerProviderError("Control owner must be 'bot' or 'user'.")
    return value


def validate_takeover_reason(reason: Any) -> str:
    value = str(reason or "").strip()
    if not value:
        raise ComputerProviderError("A reason for the takeover request is required.")
    return value[:500]


def require_bot_control(status: "ComputerStatus") -> None:
    if status.controlled_by == "user":
        raise ComputerProviderError(
            "The user currently has control of the computer. Wait for them to hand it back "
            "(they will tell you), or ask them to."
        )


class FakeComputerProvider:
    """Deterministic in-memory provider used until an isolated runtime exists."""

    provider_name = "fake"

    def __init__(self, width: int = 1920, height: int = 1080, fps: int = 30):
        self.mcp_servers: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.width = width
        self.height = height
        self.fps = fps
        self._computers: Dict[str, ComputerStatus] = {}
        self._frame_counts: Dict[str, int] = {}
        # Files the fake computer "holds": {computer_id: {path: content}}.
        self._files: Dict[str, Dict[str, str]] = {}
        # Demonstrations in progress: {computer_id: {started_at, steps, keyframes}}; tests append steps.
        self.recordings: Dict[str, Dict[str, Any]] = {}

    def get_or_create(self, bot_id: str) -> ComputerStatus:
        computer_id = _computer_id(bot_id)
        existing = self._computers.get(computer_id)
        if existing is not None:
            return existing
        status = ComputerStatus(
            computer_id=computer_id,
            bot_id=bot_id.strip(),
            provider=self.provider_name,
            state="stopped",
            health="unknown",
            width=self.width,
            height=self.height,
            fps=self.fps,
            generation=1,
            capabilities=COMPUTER_CAPABILITIES,
            updated_at=_now(),
        )
        self._computers[computer_id] = status
        self._frame_counts[computer_id] = 0
        return status

    def describe(self, bot_id: str) -> ComputerStatus:
        """Return status without creating a runtime or starting a process."""

        existing = self._computers.get(_computer_id(bot_id))
        if existing is not None:
            return existing
        return ComputerStatus(
            computer_id=_computer_id(bot_id),
            bot_id=bot_id.strip(),
            provider=self.provider_name,
            state="stopped",
            health="unknown",
            width=self.width,
            height=self.height,
            fps=self.fps,
            generation=0,
            capabilities=COMPUTER_CAPABILITIES,
            updated_at=_now(),
        )

    def _get(self, computer_id: str) -> ComputerStatus:
        if not isinstance(computer_id, str) or not computer_id.strip():
            raise ComputerProviderError("A computer id is required.")
        status = self._computers.get(computer_id)
        if status is None:
            raise ComputerProviderError("The requested computer does not exist.")
        return status

    @staticmethod
    def _touch(status: ComputerStatus, operation: str) -> ComputerStatus:
        status.last_operation = operation
        status.updated_at = _now()
        return status

    def _require_active(self, computer_id: str) -> ComputerStatus:
        status = self._get(computer_id)
        if status.state not in {"running", "paused"}:
            raise ComputerProviderError(
                f"Computer is not active; current state is {status.state}."
            )
        return status

    async def create(self, bot_id: str) -> ComputerStatus:
        return self.get_or_create(bot_id)

    async def install_packages(self, computer_id: str, packages: Any) -> Dict[str, Any]:
        status = self._require_active(computer_id)
        names = check_package_names(packages)
        status.packages = status.packages + [n for n in names if n not in status.packages]
        self._touch(status, "packages.install")
        return {
            "computer_id": status.computer_id,
            "provider": self.provider_name,
            "operation": "packages.install",
            "installed": names,
            "packages": list(status.packages),
            "output": "[fake computer] packages recorded; nothing was installed.",
        }

    async def start(self, computer_id: str) -> ComputerStatus:
        status = self._get(computer_id)
        if status.state == "running":
            return self._touch(status, "start")
        status.state = "starting"
        status.health = "unknown"
        self._touch(status, "start")
        status.state = "running"
        status.health = "healthy"
        return self._touch(status, "start")

    async def stop(self, computer_id: str) -> ComputerStatus:
        status = self._get(computer_id)
        status.state = "stopped"
        status.health = "unknown"
        status.frame_id = None
        return self._touch(status, "stop")

    async def pause(self, computer_id: str) -> ComputerStatus:
        status = self._get(computer_id)
        if status.state not in {"running", "paused"}:
            raise ComputerProviderError(
                f"Computer must be running before it can be paused; current state is {status.state}."
            )
        status.state = "paused"
        status.health = "healthy"
        return self._touch(status, "pause")

    async def reset(self, computer_id: str) -> ComputerStatus:
        status = self._get(computer_id)
        status.state = "resetting"
        status.health = "unknown"
        status.generation += 1
        status.frame_id = None
        status.url = None
        self._frame_counts[computer_id] = 0
        status.state = "stopped"
        return self._touch(status, "reset")

    async def health(self, computer_id: str) -> ComputerStatus:
        status = self._get(computer_id)
        status.health = "healthy" if status.state in {"running", "paused"} else "unknown"
        return self._touch(status, "health")

    async def browser_navigate(self, computer_id: str, url: str) -> Dict[str, Any]:
        status = self._require_active(computer_id)
        require_bot_control(status)
        if not isinstance(url, str) or len(url.strip()) > 2048:
            raise ComputerProviderError("A browser URL is required and must be at most 2048 characters.")
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ComputerProviderError("Browser navigation only accepts absolute HTTP(S) URLs.")
        status.url = url.strip()
        self._touch(status, "browser.navigate")
        return {
            "computer_id": status.computer_id,
            "provider": self.provider_name,
            "operation": "browser.navigate",
            "url": status.url,
            "state": status.state,
        }

    async def terminal_execute(self, computer_id: str, command: str, timeout_seconds: Any = None, background: bool = False) -> Dict[str, Any]:
        status = self._require_active(computer_id)
        command = check_command(command)
        limit = check_timeout(timeout_seconds)
        self._touch(status, "terminal.exec")
        if background:
            return {"computer_id": status.computer_id, "provider": self.provider_name, "operation": "terminal.exec", "command": command,
                    "background": True, "job_id": "job-fake", "pid": 0, "log": "/home/pwuser/.cubicle-jobs/job-fake.log", "exit_code": 0, "stdout": "", "stderr": ""}
        return {
            "computer_id": status.computer_id,
            "provider": self.provider_name,
            "operation": "terminal.exec",
            "command": command,
            "timeout_seconds": limit,
            "exit_code": 0,
            "stdout": "[fake computer] command accepted; no process was started.",
            "stderr": "",
        }

    async def files_list(self, computer_id: str, path: str = "/workspace") -> Dict[str, Any]:
        status = self._require_active(computer_id)
        normalized = check_file_path(path if isinstance(path, str) and path.strip() else COMPUTER_WORKSPACE)
        self._touch(status, "files.list")
        return {
            "computer_id": status.computer_id,
            "provider": self.provider_name,
            "operation": "files.list",
            "path": normalized,
            "entries": [
                {"name": "workspace", "kind": "directory"},
                {"name": "README.md", "kind": "file"},
            ],
        }

    async def files_read(self, computer_id: str, path: str) -> Dict[str, Any]:
        status = self._require_active(computer_id)
        normalized = check_file_path(path)
        content = self._files.get(computer_id, {}).get(normalized)
        if content is None:
            raise ComputerProviderError(f"No file at {normalized}.")
        self._touch(status, "files.read")
        return {
            "computer_id": status.computer_id,
            "provider": self.provider_name,
            "operation": "files.read",
            "path": normalized,
            "content": content,
            "bytes": len(content.encode("utf-8")),
            "lines": content.count("\n") + 1 if content else 0,
        }

    async def files_write(self, computer_id: str, path: str, content: str) -> Dict[str, Any]:
        status = self._require_active(computer_id)
        normalized = check_file_path(path)
        content = check_file_content(content)
        files = self._files.setdefault(computer_id, {})
        created = normalized not in files
        files[normalized] = content
        self._touch(status, "files.write")
        return {
            "computer_id": status.computer_id,
            "provider": self.provider_name,
            "operation": "files.write",
            "path": normalized,
            "created": created,
            "bytes": len(content.encode("utf-8")),
            "lines": content.count("\n") + 1 if content else 0,
        }

    async def files_edit(self, computer_id: str, path: str, old_text: str, new_text: str) -> Dict[str, Any]:
        status = self._require_active(computer_id)
        normalized = check_file_path(path)
        if not isinstance(old_text, str) or not old_text:
            raise ComputerProviderError("old_text is the exact passage to replace and cannot be empty.")
        new_text = check_file_content(new_text or "")
        current = self._files.get(computer_id, {}).get(normalized)
        if current is None:
            raise ComputerProviderError(f"No file at {normalized}.")
        count = current.count(old_text)
        if count == 0:
            raise ComputerProviderError(f"The passage to replace is not in {normalized}. Read the file and copy it exactly.")
        if count > 1:
            raise ComputerProviderError(f"The passage to replace appears {count} times in {normalized}; include more surrounding text so it matches once.")
        at = current.index(old_text)
        updated = check_file_content(current[:at] + new_text + current[at + len(old_text):])
        self._files[computer_id][normalized] = updated
        self._touch(status, "files.edit")
        return {
            "computer_id": status.computer_id,
            "provider": self.provider_name,
            "operation": "files.edit",
            "path": normalized,
            "line": current[:at].count("\n") + 1,
            "replaced": len(old_text),
            "inserted": len(new_text),
            "bytes": len(updated.encode("utf-8")),
            "lines": updated.count("\n") + 1 if updated else 0,
        }

    async def screenshot(self, computer_id: str) -> Dict[str, Any]:
        status = self._get(computer_id)
        available = status.state in {"running", "paused"}
        if available:
            self._frame_counts[computer_id] = self._frame_counts.get(computer_id, 0) + 1
            status.frame_id = f"frame-{status.generation}-{self._frame_counts[computer_id]}"
        self._touch(status, "screenshot")
        return {
            "computer_id": status.computer_id,
            "provider": self.provider_name,
            "available": available,
            "frame_id": status.frame_id,
            "format": "placeholder",
            "width": status.width,
            "height": status.height,
            "state": status.state,
            "generation": status.generation,
            "data": None,
            "message": (
                "A real screen stream is not configured."
                if not available
                else "Fake provider frame metadata; no desktop process is running."
            ),
        }

    async def send_input(self, computer_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        status = self._require_active(computer_id)
        require_bot_control(status)
        if not isinstance(event, dict):
            raise ComputerProviderError("Computer input must be a JSON object.")
        event_type = str(event.get("type") or "").lower()
        if event_type not in {"click", "keypress", "type", "scroll"}:
            raise ComputerProviderError("Supported input types are click, keypress, type, and scroll.")
        self._touch(status, "input")
        return {
            "computer_id": status.computer_id,
            "provider": self.provider_name,
            "operation": "input",
            "accepted": True,
            "type": event_type,
        }

    async def cleanup(self, computer_id: str) -> Dict[str, Any]:
        status = self._get(computer_id)
        self._computers.pop(computer_id, None)
        self._frame_counts.pop(computer_id, None)
        return {
            "computer_id": status.computer_id,
            "bot_id": status.bot_id,
            "provider": self.provider_name,
            "state": "cleaned",
            "generation": status.generation,
        }

    async def set_control(self, computer_id: str, owner: str) -> ComputerStatus:
        status = self._get(computer_id)
        status.controlled_by = validate_control_owner(owner)
        if status.controlled_by == "bot":
            status.takeover_request = None
        return self._touch(status, "control")

    async def request_takeover(self, computer_id: str, reason: str) -> Dict[str, Any]:
        status = self._require_active(computer_id)
        status.takeover_request = {"reason": validate_takeover_reason(reason), "requested_at": _now()}
        self._touch(status, "takeover.request")
        return {
            "computer_id": status.computer_id,
            "provider": self.provider_name,
            "operation": "takeover.request",
            "requested": True,
            "reason": status.takeover_request["reason"],
            "controlled_by": status.controlled_by,
            "next": "Tell the user what to do and end your turn; they will message you when they hand control back.",
        }

    async def record_start(self, computer_id: str) -> Dict[str, Any]:
        self._require_active(computer_id)
        existing = self.recordings.get(computer_id)
        if existing is None:
            existing = self.recordings[computer_id] = {"started_at": _now(), "steps": [], "keyframes": []}
        return {"computer_id": computer_id, "provider": self.provider_name, "operation": "record.start", "started_at": existing["started_at"]}

    async def record_stop(self, computer_id: str) -> Dict[str, Any]:
        recording = self.recordings.pop(computer_id, None)
        if recording is None:
            raise ComputerProviderError("Nothing is being recorded.")
        status = self._get(computer_id)
        return {
            "computer_id": computer_id,
            "provider": self.provider_name,
            "operation": "record.stop",
            "started_at": recording["started_at"],
            "duration_ms": 0,
            "width": status.width,
            "height": status.height,
            "steps": list(recording["steps"]),
            "keyframes": list(recording["keyframes"]),
            "dropped_steps": 0,
        }

    async def record_status(self, computer_id: str) -> Dict[str, Any]:
        recording = self.recordings.get(computer_id)
        return {"computer_id": computer_id, "provider": self.provider_name, "active": recording is not None, "steps": len(recording["steps"]) if recording else 0}

    def vnc_target(self, computer_id: str) -> Optional[Dict[str, Any]]:
        return None

    async def mcp_start(self, computer_id: str, server: Dict[str, Any]) -> Dict[str, Any]:
        self._require_active(computer_id)
        self.mcp_servers.setdefault(computer_id, {})[str(server.get("id"))] = dict(server)
        return {"operation": "mcp.start", "started": True, "running": True}

    async def mcp_stop(self, computer_id: str, server_id: str) -> Dict[str, Any]:
        stopped = self.mcp_servers.get(computer_id, {}).pop(server_id, None) is not None
        return {"operation": "mcp.stop", "stopped": stopped}

    def mcp_endpoint(self, computer_id: str, server_id: str) -> Optional[Dict[str, Any]]:
        status = self._computers.get(computer_id)
        if status is None or status.state != "running" or server_id not in self.mcp_servers.get(computer_id, {}):
            return None
        return {"url": f"http://fake-computer/{computer_id}/mcp/{server_id}", "headers": {}}


def build_computer_provider() -> ComputerProvider:
    """Select the configured adapter without making Docker a test prerequisite."""

    from app.config import settings

    if settings.COMPUTER_PROVIDER == "docker":
        from app.services.docker_computer_provider import DockerComputerProvider
        from app.services.secrets_service import environment_for

        # The user's named secrets become environment variables in each
        # bot's sandbox, read at every start.
        return DockerComputerProvider(environment=environment_for)
    return FakeComputerProvider()


computer_provider = build_computer_provider()
