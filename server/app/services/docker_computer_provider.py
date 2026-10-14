"""Docker-backed Playwright computer provider.

Each bot receives one short-lived container with a private browser context,
a writable workspace mount, a persistent home directory, the user's shared
folder at /shared when its host path is known, and an internal token. The
host API is the only component that knows the token or container id;
callers continue to use the provider-neutral computer contract.

What persists, and where. Beside each computer's workspace directory sit
`<id>.home` (the sandbox user's home: venvs, npm and cargo installs, dotfiles,
the browser profile), `<id>.apt` (apt's download and index caches), and
`<id>.packages` (the Ubuntu packages this computer has installed, one per
line). The container's own filesystem is disposable: system packages are
re-installed from the list at every boot, which is what lets the image be
rebuilt freely. `reset` wipes the home directory and the package list; the
workspace stays.

Two connection layouts are supported. When the API runs directly on the Docker
host, each container publishes an ephemeral port on 127.0.0.1. When the API is
itself a container (docker compose), `COMPUTER_DOCKER_NETWORK` names a Docker
network that runtime containers join, and they are reached by container name
without publishing any port.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import secrets
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.services.computer_provider import (
    check_command,
    check_file_content,
    check_file_path,
    check_package_names,
    check_timeout,
    PACKAGE_NAME,
    COMPUTER_CAPABILITIES,
    ComputerProviderError,
    ComputerStatus,
    computer_id_for_bot,
    require_bot_control,
    validate_control_owner,
    validate_takeover_reason,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


DockerCommand = Callable[[Sequence[str], float], str]

# The container starts as root to restore packages and fix up the mounted
# home, then drops to the sandbox user with an empty bounding set (see
# runtime/start.sh). These are the capabilities that root stage, and the
# root `docker exec` behind install_packages, keep: enough for dpkg and apt
# to install files and switch users, nothing that reaches the host.
SETUP_CAPABILITIES = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "SETGID", "SETUID", "SETPCAP")

INSTALL_SCRIPT = "/opt/cubicle-computer/install-packages.sh"


logger = logging.getLogger(__name__)

# Where the shared folder appears inside a computer.
SHARED_MOUNT = "/shared"
# Seconds the API waits beyond the driver's own command timeout.
RUNTIME_REPLY_MARGIN = 15.0
# What `docker ps` prints per computer container, for adoption after a restart.
_PS_FORMAT = '{{.ID}}\t{{.Names}}\t{{.State}}\t{{.Label "cubicle.computer-id"}}\t{{.Label "cubicle.bot-id"}}'


def _parse_ps(listing: str):
    for line in (listing or "").splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 5 and parts[0]:
            yield tuple(part.strip() for part in parts[:5])
@dataclass
class _RuntimeRecord:
    status: ComputerStatus
    token: str
    workspace: Path
    container_id: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    volume: Optional[str] = None


class DockerComputerProvider:
    """Run browser, terminal, file, input, and screenshot actions in Docker."""

    provider_name = "docker-playwright"

    def __init__(
        self,
        *,
        docker_binary: Optional[str] = None,
        image: Optional[str] = None,
        workspace_root: Optional[Path] = None,
        cpu_limit: Optional[str] = None,
        memory_limit: Optional[str] = None,
        pids_limit: Optional[int] = None,
        start_timeout: Optional[float] = None,
        command_timeout: Optional[float] = None,
        runtime_port: Optional[int] = None,
        seccomp_profile: Optional[Path] = None,
        network: Optional[str] = None,
        workspace_mode: Optional[str] = None,
        host_workspace_root: Optional[str] = None,
        shared_dir: Optional[str] = None,
        wallpaper: Optional[str] = None,
        tmp_size: Optional[str] = None,
        install_timeout: Optional[float] = None,
        environment: Optional[Callable[[str], Dict[str, str]]] = None,
        docker_command: Optional[DockerCommand] = None,
    ):
        self.docker_binary = docker_binary or settings.COMPUTER_DOCKER_BINARY
        self.image = image or settings.COMPUTER_DOCKER_IMAGE
        self.workspace_root = (workspace_root or settings.COMPUTER_DOCKER_WORKSPACE_ROOT).expanduser().resolve()
        self.cpu_limit = cpu_limit or settings.COMPUTER_DOCKER_CPU_LIMIT
        self.memory_limit = memory_limit or settings.COMPUTER_DOCKER_MEMORY_LIMIT
        self.pids_limit = pids_limit or settings.COMPUTER_DOCKER_PIDS_LIMIT
        self.start_timeout = start_timeout or settings.COMPUTER_DOCKER_START_TIMEOUT
        self.command_timeout = command_timeout or settings.COMPUTER_DOCKER_COMMAND_TIMEOUT
        self.command_timeout_max = max(self.command_timeout, settings.COMPUTER_DOCKER_COMMAND_TIMEOUT_MAX)
        self.navigate_timeout = settings.COMPUTER_DOCKER_NAVIGATE_TIMEOUT
        self.tmp_size = (tmp_size or settings.COMPUTER_DOCKER_TMP_SIZE).strip()
        self.install_timeout = install_timeout or settings.COMPUTER_DOCKER_INSTALL_TIMEOUT
        # Extra environment for a bot's container (the user's secrets), looked
        # up by bot id at every launch so changes apply at the next start.
        self._environment = environment
        self.runtime_port = runtime_port or settings.COMPUTER_DOCKER_RUNTIME_PORT
        self.seccomp_profile = (seccomp_profile or settings.COMPUTER_DOCKER_SECCOMP_PROFILE).expanduser().resolve()
        self.network = (network if network is not None else settings.COMPUTER_DOCKER_NETWORK).strip()
        mode = (workspace_mode or settings.COMPUTER_DOCKER_WORKSPACE_MODE or "bind").strip().lower()
        if mode not in {"bind", "volume"}:
            raise ValueError(f"Unsupported computer workspace mode: {mode}")
        self.workspace_mode = mode
        self.host_workspace_root = (
            host_workspace_root
            if host_workspace_root is not None
            else settings.COMPUTER_DOCKER_HOST_WORKSPACE_ROOT
        ).strip()
        # Host path of the shared folder, mounted at /shared in every computer.
        self.shared_dir = (shared_dir if shared_dir is not None else settings.COMPUTER_DOCKER_HOST_SHARED_DIR).strip()
        self.wallpaper = (wallpaper if wallpaper is not None else settings.COMPUTER_DOCKER_WALLPAPER).strip()
        self._docker_command = docker_command or self._run_docker
        self._runtimes: Dict[str, _RuntimeRecord] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _new_status(bot_id: str, generation: int = 0) -> ComputerStatus:
        return ComputerStatus(
            computer_id=computer_id_for_bot(bot_id),
            bot_id=bot_id.strip(),
            provider=DockerComputerProvider.provider_name,
            state="stopped",
            health="unknown",
            width=1280,
            height=720,
            fps=10,
            generation=generation,
            capabilities=COMPUTER_CAPABILITIES + ("vnc",),
            updated_at=_now(),
        )

    @staticmethod
    def _touch(status: ComputerStatus, operation: str) -> ComputerStatus:
        status.last_operation = operation
        status.updated_at = _now()
        return status

    def describe(self, bot_id: str) -> ComputerStatus:
        computer_id = computer_id_for_bot(bot_id)
        record = self._runtimes.get(computer_id)
        return record.status if record else self._new_status(bot_id)

    def get_or_create(self, bot_id: str) -> ComputerStatus:
        computer_id = computer_id_for_bot(bot_id)
        existing = self._runtimes.get(computer_id)
        if existing:
            return existing.status

        workspace = (self.workspace_root / computer_id).resolve()
        if self.workspace_root not in workspace.parents:
            raise ComputerProviderError("The computer workspace escaped its configured root.")
        workspace.mkdir(parents=True, exist_ok=True)
        self._set_workspace_permissions(workspace)
        status = self._new_status(bot_id, generation=1)
        record = _RuntimeRecord(
            status=status,
            token=secrets.token_urlsafe(32),
            workspace=workspace,
            volume=self._volume_name(computer_id) if self.workspace_mode == "volume" else None,
        )
        status.packages = self._read_packages(record)
        self._runtimes[computer_id] = record
        return status

    # Persistent state beside the workspace directory.

    @staticmethod
    def _home_dir(record: _RuntimeRecord) -> Path:
        return record.workspace.with_name(record.workspace.name + ".home")

    @staticmethod
    def _apt_dir(record: _RuntimeRecord) -> Path:
        return record.workspace.with_name(record.workspace.name + ".apt")

    @staticmethod
    def _packages_file(record: _RuntimeRecord) -> Path:
        return record.workspace.with_name(record.workspace.name + ".packages")

    def _read_packages(self, record: _RuntimeRecord) -> list:
        try:
            text = self._packages_file(record).read_text(encoding="utf-8")
        except OSError:
            return []
        names = []
        for line in text.splitlines():
            name = line.strip().lower()
            if name and not name.startswith("#") and PACKAGE_NAME.match(name) and name not in names:
                names.append(name)
        return names

    def _write_packages(self, record: _RuntimeRecord, names: list) -> None:
        path = self._packages_file(record)
        try:
            if names:
                path.write_text("".join(f"{name}\n" for name in names), encoding="utf-8")
            elif path.exists():
                path.unlink()
        except OSError as exc:
            raise ComputerProviderError(f"Could not update the package list: {exc}") from exc
        record.status.packages = list(names)

    def _record_for(self, computer_id: str) -> _RuntimeRecord:
        record = self._runtimes.get(computer_id)
        if record is None:
            raise ComputerProviderError("The requested computer does not exist.")
        return record

    def _active_record(self, computer_id: str) -> _RuntimeRecord:
        record = self._record_for(computer_id)
        if record.status.state not in {"running", "paused"}:
            raise ComputerProviderError(
                f"Computer is not active; current state is {record.status.state}."
            )
        if not record.container_id or not record.port or not record.host:
            raise ComputerProviderError("Computer runtime connection is unavailable.")
        return record

    def _run_docker(self, args: Sequence[str], timeout: float) -> str:
        try:
            completed = subprocess.run(
                [self.docker_binary, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ComputerProviderError(
                f"Docker executable not found: {self.docker_binary}."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ComputerProviderError("Docker command timed out.") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "Docker command failed.").strip()
            raise ComputerProviderError(f"Docker command failed: {detail[:600]}")
        return completed.stdout.strip()

    async def _docker(self, args: Sequence[str], timeout: Optional[float] = None) -> str:
        return await asyncio.to_thread(
            self._docker_command,
            tuple(args),
            timeout if timeout is not None else self.command_timeout,
        )

    def _container_name(self, record: _RuntimeRecord) -> str:
        return f"cubicle-computer-{record.status.computer_id[-70:]}"

    @staticmethod
    def _volume_name(computer_id: str, kind: str = "ws") -> str:
        return f"cubicle-computer-{kind}-{computer_id[-60:]}"

    def _network_args(self) -> list:
        if self.network:
            return ["--network", self.network]
        return ["--publish", f"127.0.0.1::{self.runtime_port}"]

    def _host_path(self, path: Path) -> Path:
        if self.host_workspace_root:
            # The daemon resolves bind sources on the host, so translate the
            # path the API sees into the equivalent host path.
            return Path(self.host_workspace_root) / path.name
        return path

    def _mount(self, record: _RuntimeRecord, kind: str, source: Path, destination: str) -> str:
        if self.workspace_mode == "volume":
            return f"type=volume,src={self._volume_name(record.status.computer_id, kind)},dst={destination}"
        return f"type=bind,src={self._host_path(source)},dst={destination}"

    def _workspace_mount(self, record: _RuntimeRecord) -> str:
        return self._mount(record, "ws", record.workspace, "/workspace")

    def _state_mounts(self, record: _RuntimeRecord) -> list:
        """The sandbox user's home and apt's caches, both kept between starts."""
        if self.workspace_mode == "bind":
            for path in (self._home_dir(record), self._apt_dir(record)):
                path.mkdir(parents=True, exist_ok=True)
        return [
            "--mount",
            self._mount(record, "home", self._home_dir(record), "/home/pwuser"),
            "--mount",
            self._mount(record, "apt", self._apt_dir(record), "/var/lib/cubicle/apt"),
        ]

    @staticmethod
    def _runtime_url(record: _RuntimeRecord, route: str) -> str:
        return f"http://{record.host}:{record.port}{route}"

    def _shared_args(self) -> list:
        """The user's shared folder, the same directory the host-side tools use.

        It is a plain bind mount even in volume mode: the point is that the
        user (and every other bot) sees the same files from outside.
        """
        if not self.shared_dir:
            return []
        return ["--mount", f"type=bind,src={self.shared_dir},dst={SHARED_MOUNT}", "--env", f"SHARED={SHARED_MOUNT}"]

    def _wallpaper_args(self) -> list:
        if not self.wallpaper:
            return []
        # Mounted over the image's generated default; start.sh applies it.
        return ["--mount", f"type=bind,src={self.wallpaper},dst=/opt/cubicle-computer/wallpaper,readonly"]

    # Names the runtime sets itself; a secret may not replace them.
    _OWN_ENV = frozenset({"COMPUTER_TOKEN", "COMMAND_TIMEOUT_MS", "COMMAND_TIMEOUT_MAX_MS", "NAVIGATE_TIMEOUT_MS", "COMPUTER_ID", "PORT", "WORKSPACE", "SHARED", "CUBICLE_PACKAGES", "HOME", "PATH", "DISPLAY"})

    def _secret_env_args(self, record: _RuntimeRecord) -> list:
        if self._environment is None:
            return []
        args = []
        for name, value in sorted(self._environment(record.status.bot_id).items()):
            if name in self._OWN_ENV or not isinstance(value, str):
                continue
            args.extend(["--env", f"{name}={value}"])
        return args

    async def _remove_stale_container(self, record: _RuntimeRecord) -> None:
        """Remove a leftover container that still holds this computer's name.

        The API keeps runtime records in memory, so after a restart a sandbox
        started by the previous process is unknown to it (and its token is
        gone). Without this, `docker run --name` fails with a name conflict
        and the computer is stuck in the error state until someone cleans up
        by hand.
        """
        try:
            await self._docker(["rm", "-f", self._container_name(record)])
        except ComputerProviderError:
            pass  # Nothing with that name exists; that is the normal case.

    def _set_workspace_permissions(self, workspace: Path) -> None:
        # In bind mode the directory is shared with the sandbox, which runs
        # as its own unprivileged user (not the API's), so it must be
        # writable across users. In volume mode it is private to the API.
        try:
            workspace.chmod(0o777 if self.workspace_mode == "bind" else 0o700)
        except OSError:
            pass  # Some filesystems (and Windows) do not support this; not fatal.

    async def _launch(self, record: _RuntimeRecord) -> None:
        record.workspace.mkdir(parents=True, exist_ok=True)
        self._set_workspace_permissions(record.workspace)
        await self._remove_stale_container(record)
        args = [
            "run",
            "-d",
            "--rm",
            "--init",
            "--name",
            self._container_name(record),
            "--label",
            "cubicle.runtime=computer",
            "--label",
            f"cubicle.computer-id={record.status.computer_id}",
            "--label",
            f"cubicle.bot-id={record.status.bot_id}",
            *self._network_args(),
            # The root filesystem is the container's own writable layer so
            # apt can install into it; it is thrown away with the container
            # and rebuilt from the package list at the next start.
            "--tmpfs",
            f"/tmp:rw,nosuid,size={self.tmp_size}",
            "--mount",
            self._workspace_mount(record),
            *self._state_mounts(record),
            *self._shared_args(),
            *self._wallpaper_args(),
            "--cpus",
            self.cpu_limit,
            "--memory",
            self.memory_limit,
            "--pids-limit",
            str(self.pids_limit),
            "--cap-drop",
            "ALL",
            *[arg for cap in SETUP_CAPABILITIES for arg in ("--cap-add", cap)],
            "--security-opt",
            "no-new-privileges:true",
            "--shm-size",
            "2g",
            "--env",
            f"COMPUTER_TOKEN={record.token}",
            "--env",
            f"COMPUTER_ID={record.status.computer_id}",
            "--env",
            f"PORT={self.runtime_port}",
            "--env",
            "WORKSPACE=/workspace",
            "--env",
            f"COMMAND_TIMEOUT_MS={int(self.command_timeout * 1000)}",
            "--env",
            f"COMMAND_TIMEOUT_MAX_MS={int(self.command_timeout_max * 1000)}",
            "--env",
            f"NAVIGATE_TIMEOUT_MS={int(self.navigate_timeout * 1000)}",
            "--env",
            f"CUBICLE_PACKAGES={' '.join(record.status.packages)}",
            *self._secret_env_args(record),
            self.image,
        ]
        if self.seccomp_profile.is_file():
            args[-1:-1] = ["--security-opt", f"seccomp={self.seccomp_profile}"]

        output = await self._docker(args, timeout=self.start_timeout)
        container_id = output.splitlines()[-1].strip() if output else ""
        if not container_id:
            raise ComputerProviderError("Docker did not return a computer container id.")
        record.container_id = container_id

        if self.network:
            # On a user-defined network the container name resolves through
            # Docker's embedded DNS, so nothing is published.
            record.host = self._container_name(record)
            record.port = self.runtime_port
            return

        ports = await self._docker(["port", container_id, f"{self.runtime_port}/tcp"])
        matches = re.findall(r":(\d+)", ports)
        if not matches:
            raise ComputerProviderError("Docker did not publish a computer runtime port.")
        record.host = "127.0.0.1"
        record.port = int(matches[-1])

    async def _remove_container(self, record: _RuntimeRecord, suppress_errors: bool = True) -> None:
        if not record.container_id:
            record.host = None
            record.port = None
            return
        container_id = record.container_id
        try:
            await self._docker(["rm", "-f", container_id])
        except ComputerProviderError:
            if not suppress_errors:
                raise
        finally:
            record.container_id = None
            record.host = None
            record.port = None

    async def _request(
        self,
        record: _RuntimeRecord,
        method: str,
        route: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not record.port or not record.host:
            raise ComputerProviderError("Computer runtime is not connected.")
        url = self._runtime_url(record, route)
        try:
            # The driver enforces command_timeout itself and answers with what
            # it has; the API must outwait that, or a command that runs to the
            # limit is misreported as a dead runtime.
            async with httpx.AsyncClient(
                timeout=(timeout if timeout is not None else self.command_timeout) + RUNTIME_REPLY_MARGIN,
                headers={"x-computer-token": record.token},
            ) as client:
                response = await client.request(method, url, json=payload)
        except httpx.HTTPError as exc:
            raise ComputerProviderError("Computer runtime did not respond.") from exc

        try:
            result = response.json()
        except ValueError as exc:
            raise ComputerProviderError("Computer runtime returned invalid JSON.") from exc
        if response.status_code >= 400:
            raise ComputerProviderError(str(result.get("error") or "Computer runtime action failed."))
        if not isinstance(result, dict):
            raise ComputerProviderError("Computer runtime returned an invalid payload.")
        return result

    async def _wait_until_ready(self, record: _RuntimeRecord) -> Dict[str, Any]:
        # Packages are restored before the desktop comes up, so a computer
        # with a package list is allowed the install budget as well.
        budget = self.start_timeout + (self.install_timeout if record.status.packages else 0)
        deadline = asyncio.get_running_loop().time() + budget
        last_error = "Computer runtime did not become ready."
        while asyncio.get_running_loop().time() < deadline:
            try:
                health = await self._request(record, "GET", "/health")
                if health.get("status") == "healthy":
                    return health
            except ComputerProviderError as exc:
                last_error = str(exc)
            await asyncio.sleep(0.2)
        raise ComputerProviderError(last_error)


    # ---- adopting sandboxes from a previous API process ----------------------

    async def adopt_running(self) -> List[str]:
        """Take over every computer container a previous API process left running.

        Runtime records live in memory, so after a restart the API would
        otherwise report every computer as stopped and the bot's next action
        would replace a perfectly good sandbox (losing /tmp, the browser and
        anything running). The token the driver expects is in the container's
        environment, so the record can be rebuilt. Returns the computer ids.
        """
        try:
            listing = await self._docker(["ps", "--filter", "label=cubicle.runtime=computer", "--format", _PS_FORMAT])
        except ComputerProviderError as exc:
            logger.warning("Could not list computer containers to adopt: %s", exc)
            return []
        adopted: List[str] = []
        for container_id, name, state, computer_id, bot_id in _parse_ps(listing):
            existing = self._runtimes.get(computer_id)
            if existing is not None and existing.container_id:
                continue
            if await self._adopt(container_id, name, state, computer_id, bot_id):
                adopted.append(computer_id)
        return adopted

    async def _adopt_for(self, record: _RuntimeRecord) -> bool:
        """Adopt this computer's container if one is running (a start after a restart)."""
        try:
            listing = await self._docker(
                ["ps", "--filter", f"label=cubicle.computer-id={record.status.computer_id}", "--format", _PS_FORMAT]
            )
        except ComputerProviderError:
            return False
        for container_id, name, state, computer_id, bot_id in _parse_ps(listing):
            if computer_id == record.status.computer_id:
                return await self._adopt(container_id, name, state, computer_id, bot_id)
        return False

    async def _adopt(self, container_id: str, name: str, state: str, computer_id: str, bot_id: str) -> bool:
        if state not in {"running", "paused"} or not bot_id:
            return False
        try:
            env_text = await self._docker(["inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", container_id])
        except ComputerProviderError:
            return False
        env = dict(line.split("=", 1) for line in env_text.splitlines() if "=" in line)
        token = env.get("COMPUTER_TOKEN", "")
        if not token:
            return False
        status = self.get_or_create(bot_id)
        record = self._runtimes[computer_id]
        record.token = token
        record.container_id = container_id
        try:
            port = int(env.get("PORT") or self.runtime_port)
        except ValueError:
            port = self.runtime_port
        if self.network:
            record.host = name.lstrip("/")
            record.port = port
        else:
            try:
                ports = await self._docker(["port", container_id, f"{port}/tcp"])
            except ComputerProviderError:
                ports = ""
            matches = re.findall(r":(\d+)", ports)
            if not matches:
                self._forget(record)
                return False
            record.host = "127.0.0.1"
            record.port = int(matches[-1])
        if state == "paused":
            status.state = "paused"
            status.health = "unknown"
            self._touch(status, "adopt")
            return True
        try:
            health = await self._request(record, "GET", "/health")
        except ComputerProviderError:
            health = {}
        if health.get("status") != "healthy":
            # Not answering with its own token: leave it for start() to replace.
            self._forget(record)
            return False
        status.state = "running"
        status.health = "healthy"
        status.width = int(health.get("width") or status.width)
        status.height = int(health.get("height") or status.height)
        self._touch(status, "adopt")
        logger.info("Adopted running computer %s for bot %s (container %s)", computer_id, bot_id, container_id[:12])
        return True

    @staticmethod
    def _forget(record: _RuntimeRecord) -> None:
        record.container_id = None
        record.host = None
        record.port = None
        record.status.state = "stopped"
        record.status.health = "unknown"


    async def create(self, bot_id: str) -> ComputerStatus:
        return self.get_or_create(bot_id)

    async def start(self, computer_id: str) -> ComputerStatus:
        async with self._lock:
            record = self._record_for(computer_id)
            status = record.status
            if status.state == "running":
                return self._touch(status, "start")
            if status.state == "paused" and record.container_id:
                await self._docker(["unpause", record.container_id])
                status.state = "running"
                status.health = "healthy"
                return self._touch(status, "start")

            # A sandbox from before an API restart is still there: use it.
            if not record.container_id and await self._adopt_for(record):
                return self._touch(status, "start")

            status.state = "starting"
            status.health = "unknown"
            self._touch(status, "start")
            try:
                record.token = secrets.token_urlsafe(32)
                await self._launch(record)
                health = await self._wait_until_ready(record)
            except Exception as exc:
                await self._remove_container(record)
                status.state = "error"
                status.health = "unhealthy"
                self._touch(status, "start")
                if isinstance(exc, ComputerProviderError):
                    raise
                raise ComputerProviderError(str(exc)) from exc

            status.state = "running"
            status.health = "healthy"
            status.width = int(health.get("width") or status.width)
            status.height = int(health.get("height") or status.height)
            return self._touch(status, "start")

    async def stop(self, computer_id: str) -> ComputerStatus:
        async with self._lock:
            record = self._record_for(computer_id)
            await self._remove_container(record)
            status = record.status
            status.state = "stopped"
            status.health = "unknown"
            status.frame_id = None
            return self._touch(status, "stop")

    async def pause(self, computer_id: str) -> ComputerStatus:
        async with self._lock:
            record = self._active_record(computer_id)
            if record.status.state == "paused":
                return self._touch(record.status, "pause")
            await self._docker(["pause", record.container_id or ""])
            record.status.state = "paused"
            record.status.health = "healthy"
            return self._touch(record.status, "pause")

    async def _wipe_home(self, record: _RuntimeRecord) -> None:
        """Return the home directory to the image's skeleton.

        Its files belong to the sandbox user, not the API, so the deletion
        runs in a throwaway container over the same mount. The apt caches
        are kept: they only make the next install faster.
        """
        if self.workspace_mode == "volume":
            try:
                await self._docker(["volume", "rm", "-f", self._volume_name(record.status.computer_id, "home")])
            except ComputerProviderError:
                pass  # The volume is created on first start; it may not exist yet.
            return
        home = self._home_dir(record)
        if not home.is_dir():
            return
        await self._docker(
            [
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "root",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "DAC_OVERRIDE",
                "--cap-add",
                "FOWNER",
                "--security-opt",
                "no-new-privileges:true",
                "--entrypoint",
                "find",
                "--mount",
                f"type=bind,src={self._host_path(home)},dst=/wipe",
                self.image,
                "/wipe",
                "-mindepth",
                "1",
                "-delete",
            ],
            timeout=self.start_timeout,
        )

    async def reset(self, computer_id: str) -> ComputerStatus:
        async with self._lock:
            record = self._record_for(computer_id)
            await self._remove_container(record)
            await self._wipe_home(record)
            self._write_packages(record, [])
            status = record.status
            status.generation += 1
            status.state = "stopped"
            status.health = "unknown"
            status.frame_id = None
            status.url = None
            record.token = secrets.token_urlsafe(32)
            return self._touch(status, "reset")

    async def health(self, computer_id: str) -> ComputerStatus:
        record = self._record_for(computer_id)
        status = record.status
        if status.state not in {"running", "paused"} or not record.container_id:
            status.health = "unknown"
            return self._touch(status, "health")
        try:
            result = await self._request(record, "GET", "/health")
            status.health = "healthy" if result.get("status") == "healthy" else "unhealthy"
            status.width = int(result.get("width") or status.width)
            status.height = int(result.get("height") or status.height)
        except ComputerProviderError:
            status.health = "unhealthy"
        return self._touch(status, "health")

    async def browser_navigate(self, computer_id: str, url: str) -> Dict[str, Any]:
        record = self._active_record(computer_id)
        require_bot_control(record.status)
        if not isinstance(url, str) or len(url.strip()) > 2048:
            raise ComputerProviderError("A browser URL is required and must be at most 2048 characters.")
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ComputerProviderError("Browser navigation only accepts absolute HTTP(S) URLs.")
        result = await self._request(record, "POST", "/navigate", {"url": url.strip()}, timeout=self.navigate_timeout)
        record.status.url = result.get("url") or url.strip()
        self._touch(record.status, "browser.navigate")
        return {"computer_id": computer_id, "provider": self.provider_name, **result}

    async def terminal_execute(self, computer_id: str, command: str, timeout_seconds: Any = None) -> Dict[str, Any]:
        record = self._active_record(computer_id)
        command = check_command(command)
        limit = min(check_timeout(timeout_seconds), self.command_timeout_max)
        result = await self._request(
            record, "POST", "/terminal", {"command": command, "timeout_ms": int(limit * 1000)}, timeout=limit
        )
        self._touch(record.status, "terminal.exec")
        return {"computer_id": computer_id, "provider": self.provider_name, **result}

    async def files_list(self, computer_id: str, path: str = "/workspace") -> Dict[str, Any]:
        record = self._active_record(computer_id)
        if not isinstance(path, str) or len(path) > 512 or not path.strip().startswith("/"):
            raise ComputerProviderError("Computer file paths must be absolute and at most 512 characters.")
        result = await self._request(record, "POST", "/files", {"path": path.strip()})
        self._touch(record.status, "files.list")
        return {"computer_id": computer_id, "provider": self.provider_name, **result}

    async def files_read(self, computer_id: str, path: str) -> Dict[str, Any]:
        record = self._active_record(computer_id)
        result = await self._request(record, "POST", "/files/read", {"path": check_file_path(path)})
        self._touch(record.status, "files.read")
        return {"computer_id": computer_id, "provider": self.provider_name, **result}

    async def files_write(self, computer_id: str, path: str, content: str) -> Dict[str, Any]:
        record = self._active_record(computer_id)
        payload = {"path": check_file_path(path), "content": check_file_content(content)}
        result = await self._request(record, "POST", "/files/write", payload)
        self._touch(record.status, "files.write")
        return {"computer_id": computer_id, "provider": self.provider_name, **result}

    async def files_edit(self, computer_id: str, path: str, old_text: str, new_text: str) -> Dict[str, Any]:
        record = self._active_record(computer_id)
        if not isinstance(old_text, str) or not old_text:
            raise ComputerProviderError("old_text is the exact passage to replace and cannot be empty.")
        payload = {"path": check_file_path(path), "old_text": old_text, "new_text": check_file_content(new_text or "")}
        result = await self._request(record, "POST", "/files/edit", payload)
        self._touch(record.status, "files.edit")
        return {"computer_id": computer_id, "provider": self.provider_name, **result}

    async def screenshot(self, computer_id: str) -> Dict[str, Any]:
        record = self._record_for(computer_id)
        if record.status.state not in {"running", "paused"}:
            return {
                "computer_id": computer_id,
                "provider": self.provider_name,
                "available": False,
                "frame_id": None,
                "format": "jpeg",
                "width": record.status.width,
                "height": record.status.height,
                "state": record.status.state,
                "data": None,
                "message": "Start the Docker computer before requesting a screen frame.",
            }
        result = await self._request(record, "POST", "/screenshot")
        record.status.frame_id = result.get("frame_id")
        record.status.width = int(result.get("width") or record.status.width)
        record.status.height = int(result.get("height") or record.status.height)
        self._touch(record.status, "screenshot")
        return {"computer_id": computer_id, "provider": self.provider_name, "available": True, **result}

    async def send_input(self, computer_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        record = self._active_record(computer_id)
        require_bot_control(record.status)
        if not isinstance(event, dict):
            raise ComputerProviderError("Computer input must be a JSON object.")
        result = await self._request(record, "POST", "/input", {"event": event})
        self._touch(record.status, "input")
        return {"computer_id": computer_id, "provider": self.provider_name, **result}

    async def set_control(self, computer_id: str, owner: str) -> ComputerStatus:
        record = self._record_for(computer_id)
        record.status.controlled_by = validate_control_owner(owner)
        if record.status.controlled_by == "bot":
            record.status.takeover_request = None
        return self._touch(record.status, "control")

    async def request_takeover(self, computer_id: str, reason: str) -> Dict[str, Any]:
        record = self._active_record(computer_id)
        record.status.takeover_request = {"reason": validate_takeover_reason(reason), "requested_at": _now()}
        self._touch(record.status, "takeover.request")
        return {
            "computer_id": computer_id,
            "provider": self.provider_name,
            "operation": "takeover.request",
            "requested": True,
            "reason": record.status.takeover_request["reason"],
            "controlled_by": record.status.controlled_by,
            "next": "Tell the user what to do and end your turn; they will message you when they hand control back.",
        }

    async def install_packages(self, computer_id: str, packages: Any) -> Dict[str, Any]:
        """Install Ubuntu packages into the running sandbox and remember them.

        Runs as root through `docker exec`; the sandbox user itself has no
        way to escalate. Names that succeed are added to the computer's
        package list, which every later start restores.
        """
        record = self._active_record(computer_id)
        names = check_package_names(packages)
        output = await self._docker(
            ["exec", "--user", "root", record.container_id or "", INSTALL_SCRIPT, *names],
            timeout=self.install_timeout,
        )
        current = self._read_packages(record)
        self._write_packages(record, current + [name for name in names if name not in current])
        self._touch(record.status, "packages.install")
        return {
            "computer_id": computer_id,
            "provider": self.provider_name,
            "operation": "packages.install",
            "installed": names,
            "packages": list(record.status.packages),
            "output": output[-4000:],
        }

    def vnc_target(self, computer_id: str) -> Optional[Dict[str, Any]]:
        record = self._runtimes.get(computer_id)
        if record is None or record.status.state not in {"running", "paused"}:
            return None
        if not record.host or not record.port or not record.container_id:
            return None
        return {"host": record.host, "port": record.port, "token": record.token}

    async def cleanup(self, computer_id: str) -> Dict[str, Any]:
        async with self._lock:
            record = self._record_for(computer_id)
            await self._remove_container(record)
            self._runtimes.pop(computer_id, None)
            return {
                "computer_id": computer_id,
                "bot_id": record.status.bot_id,
                "provider": self.provider_name,
                "state": "cleaned",
                "generation": record.status.generation,
                "workspace_preserved": True,
            }
