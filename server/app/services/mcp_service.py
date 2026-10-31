"""MCP servers: local or remote tool servers the bots can use.

A server is configured once (App Settings, or by a bot through the mcp_*
tools after approval) and kept in the settings store under `mcp_servers`:
a name, a transport (a command to run over stdio, or a URL: streamable HTTP,
or the older SSE protocol, tried in turn), environment variables or headers, and whether its tools may be
trusted as read-only. Any env or header value of the form `$NAME` is a
reference to an app secret, resolved when the server starts and never
stored resolved.

Connections are long-lived: a stdio server is one subprocess for the life
of the API, an HTTP server one session. The first use starts it (so does
startup, in the background); a failure is remembered on the server's
status and the next use tries again. Tools are listed once per connection
and cached, so a turn's tool list is stable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.config import settings
from app.services.identity import LOCAL_USER_ID, acting_for_owner, get_current_owner, owner_context
from app.services.storage_service import StorageService, current_storage, storage_service

logger = logging.getLogger(__name__)

TRANSPORTS = ("stdio", "http", "sse", "computer")
# A URL server ("http") speaks streamable HTTP or the older SSE protocol. Both are tried while
# connecting, SSE first when the path says so (.../sse, .../sse/...); "sse" is SSE only.
_SSE_PATH = re.compile(r"/sse(?:/|$)")
# An SSE stream may stay quiet between calls; it is not dropped for that.
SSE_READ_TIMEOUT = 24 * 3600.0
# A URL server whose session ended under it (restarted, most often) is connected again at once,
# then after these waits (seconds) while it is still away, then every minute.
RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
MAX_SERVERS = 30
MAX_NAME_CHARS = 60
MAX_ARGS = 40
MAX_ENV = 30
# Which of a server's tools it offers: all, only the named ones, or all but the named ones.
TOOL_MODES = ("all", "only", "except")
MAX_TOOL_NAMES = 1000
_SECRET_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]{0,63})\}|\$([A-Z][A-Z0-9_]{0,63})\b")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_ID = re.compile(r"^mcp-[a-f0-9]{8}$")
# What a literal env or header value is shown as: it may be a key typed in instead of a $NAME
# reference, so it never goes back out. Sent back unchanged, it keeps the stored value.
HIDDEN = "********"
# What a stdio server's process inherits besides its own variables. Secrets
# reach it only through explicit references.
_INHERITED_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA", "USERPROFILE")
CONNECT_TIMEOUT = float(os.getenv("MCP_CONNECT_TIMEOUT", "60"))
CALL_TIMEOUT = float(os.getenv("MCP_CALL_TIMEOUT", "120"))


class McpError(RuntimeError):
    pass


# ---- configuration ------------------------------------------------------------


def _clean_name(name: Any) -> str:
    text = " ".join(str(name or "").split())
    if not text:
        raise McpError("A server needs a name.")
    if len(text) > MAX_NAME_CHARS:
        raise McpError(f"The name is limited to {MAX_NAME_CHARS} characters.")
    return text


def _clean_args(args: Any) -> List[str]:
    if args is None or args == "":
        return []
    if isinstance(args, str):
        import shlex

        try:
            args = shlex.split(args)
        except ValueError as exc:
            raise McpError(f"Could not parse the arguments: {exc}") from exc
    if not isinstance(args, (list, tuple)):
        raise McpError("'args' must be a list of strings.")
    cleaned = [str(a) for a in args]
    if len(cleaned) > MAX_ARGS:
        raise McpError(f"At most {MAX_ARGS} arguments.")
    return cleaned


def _clean_map(values: Any, label: str) -> Dict[str, str]:
    if values is None or values == "":
        return {}
    if isinstance(values, str):
        pairs = {}
        for line in values.replace(";", "\n").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                pairs[key.strip()] = value.strip()
        values = pairs
    if not isinstance(values, dict):
        raise McpError(f"'{label}' must be a mapping of names to values.")
    cleaned: Dict[str, str] = {}
    for key, value in values.items():
        name = str(key).strip()
        if not _ENV_NAME.fullmatch(name):
            raise McpError(f"{label}: {name!r} is not a valid name.")
        cleaned[name] = str(value if value is not None else "")
    if len(cleaned) > MAX_ENV:
        raise McpError(f"{label}: at most {MAX_ENV} entries.")
    return cleaned


def clean_server(data: Any, *, existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A valid server record from user or bot input (merged over `existing` for edits)."""
    if not isinstance(data, dict):
        raise McpError("A server definition must be an object.")
    base = dict(existing or {})
    merged = {**base, **{k: v for k, v in data.items() if v is not None}}
    transport = str(merged.get("transport") or ("http" if merged.get("url") else "stdio")).strip().lower()
    if transport not in TRANSPORTS:
        raise McpError("'transport' must be stdio (a command), http (a URL), sse (a URL speaking only the older SSE protocol), or computer (a command run inside the bot's computer).")
    record: Dict[str, Any] = {
        "id": base.get("id") or f"mcp-{uuid.uuid4().hex[:8]}",
        "name": _clean_name(merged.get("name")),
        "transport": transport,
        "command": "",
        "args": [],
        "env": {},
        "url": "",
        "headers": {},
        "enabled": bool(merged.get("enabled", True)),
        "trust_read_only": bool(merged.get("trust_read_only", False)),
        **_clean_tool_filter(merged.get("tool_mode"), merged.get("tool_names")),
        "created_by": base.get("created_by") or (str(merged.get("created_by") or "user")),
        "created_at": base.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if transport in ("stdio", "computer"):
        command = str(merged.get("command") or "").strip()
        if not command:
            raise McpError("A stdio server needs the command to run, e.g. npx.")
        if len(command) > 300 or any(ch in command for ch in "\n\r\0"):
            raise McpError("The command looks wrong.")
        record["command"] = command
        record["args"] = _clean_args(merged.get("args"))
        record["env"] = _kept_hidden(_clean_map(merged.get("env"), "env"), base.get("env"), "env")
    else:
        url = str(merged.get("url") or "").strip()
        if not re.match(r"^https?://[^\s]+$", url) or len(url) > 500:
            raise McpError(f"An {transport} server needs an http(s) URL.")
        record["url"] = url
        record["headers"] = _kept_hidden(_clean_map(merged.get("headers"), "headers"), base.get("headers"), "headers")
    return record


def _clean_tool_filter(mode: Any, names: Any) -> Dict[str, Any]:
    mode = str(mode or "all").strip().lower()
    if mode not in TOOL_MODES:
        raise McpError("'tool_mode' must be all, only or except.")
    if mode == "all":
        return {"tool_mode": "all", "tool_names": []}
    if isinstance(names, str):
        names = [n for n in re.split(r"[,\n]", names)]
    if not isinstance(names, (list, tuple)):
        raise McpError("'tool_names' must be a list of the server's tool names.")
    cleaned: List[str] = []
    for name in names:
        text = str(name or "").strip()
        if text and len(text) <= 200 and text not in cleaned:
            cleaned.append(text)
    if len(cleaned) > MAX_TOOL_NAMES:
        raise McpError(f"At most {MAX_TOOL_NAMES} tool names.")
    return {"tool_mode": mode, "tool_names": cleaned}


def offers(record: Dict[str, Any], tool_name: str) -> bool:
    """Whether the server offers this tool of its own (by the name it lists), per its tool filter."""
    mode = record.get("tool_mode") or "all"
    if mode == "only":
        return tool_name in (record.get("tool_names") or [])
    if mode == "except":
        return tool_name not in (record.get("tool_names") or [])
    return True


def _kept_hidden(values: Dict[str, str], stored: Any, label: str) -> Dict[str, str]:
    """An edit sends hidden values back as HIDDEN: those keep what is stored under the same name."""
    stored = stored if isinstance(stored, dict) else {}
    for name, value in values.items():
        if value == HIDDEN:
            if name not in stored:
                raise McpError(f"{label}: enter the value for {name}.")
            values[name] = stored[name]
    return values


def _shown(value: Any) -> str:
    text = str(value or "")
    return text if not text or _refs_in(text) else HIDDEN


def _refs_in(value: str) -> List[str]:
    return [a or b for a, b in _SECRET_REF.findall(str(value))]


def secret_references(record: Dict[str, Any]) -> List[str]:
    refs: List[str] = []
    for value in list((record.get("env") or {}).values()) + list((record.get("headers") or {}).values()):
        for name in _refs_in(value):
            if name not in refs:
                refs.append(name)
    return refs


def resolve_values(values: Dict[str, str], secrets: Dict[str, str]) -> Dict[str, str]:
    """`$NAME` or `${NAME}` anywhere in a value becomes the app secret NAME."""
    resolved: Dict[str, str] = {}
    for key, value in values.items():
        text = str(value)
        for name in _refs_in(text):
            if name not in secrets:
                raise McpError(f"The secret {name} is not set; add it under Secrets in App Settings (or, for a bot's own server, in the bot's settings).")

        def _sub(match: "re.Match[str]") -> str:
            return secrets[match.group(1) or match.group(2)]

        resolved[key] = _SECRET_REF.sub(_sub, text)
    return resolved


def public(record: Dict[str, Any]) -> Dict[str, Any]:
    """The record as the API and the model see it: secret references stay references, and a value
    without one (a key typed in directly) is shown as HIDDEN."""
    return {
        "id": record["id"],
        "name": record["name"],
        "transport": record["transport"],
        "command": record.get("command") or "",
        "args": list(record.get("args") or []),
        "env": {name: _shown(value) for name, value in (record.get("env") or {}).items()},
        "url": record.get("url") or "",
        "headers": {name: _shown(value) for name, value in (record.get("headers") or {}).items()},
        "enabled": bool(record.get("enabled", True)),
        "trust_read_only": bool(record.get("trust_read_only", False)),
        "tool_mode": record.get("tool_mode") or "all",
        "tool_names": list(record.get("tool_names") or []),
        "created_by": record.get("created_by") or "user",
        "created_at": record.get("created_at"),
        "secret_references": secret_references(record),
        "scope": record.get("scope", "system"),
        "bot_id": record.get("bot_id"),
    }


SCOPES = ("system", "user")
SCOPE_KEYS = {"system": "mcp_servers", "user": "user_mcp_servers"}


class McpStore:
    """The configured servers of one scope: `system` servers live in the
    app-wide settings (admins manage them, every bot gets them); `user`
    servers in the current account's own settings."""

    def __init__(self, storage: Optional[StorageService] = None, scope: str = "system"):
        if scope not in SCOPES:
            raise McpError("scope must be system or user.")
        self._storage = storage
        self.scope = scope
        self.key = SCOPE_KEYS[scope]

    @property
    def storage(self) -> StorageService:
        return self._storage or current_storage()

    def list(self) -> List[Dict[str, Any]]:
        raw = self.storage.get_settings().get(self.key) or []
        servers = []
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict) and _ID.fullmatch(str(item.get("id") or "")):
                servers.append({**item, "scope": self.scope})
        return servers

    def get(self, server_id: str) -> Dict[str, Any]:
        for item in self.list():
            if item["id"] == server_id:
                return item
        raise McpError("There is no MCP server with that id.")

    def _save(self, servers: List[Dict[str, Any]]) -> None:
        self.storage.save_settings({self.key: [{k: v for k, v in s.items() if k != "scope"} for s in servers]})

    def add(self, data: Any, *, created_by: str = "user") -> Dict[str, Any]:
        servers = self.list()
        if len(servers) >= MAX_SERVERS:
            raise McpError(f"At most {MAX_SERVERS} MCP servers.")
        record = clean_server({**(data if isinstance(data, dict) else {}), "created_by": created_by})
        if any(s["name"].lower() == record["name"].lower() for s in servers):
            raise McpError(f"A server named {record['name']} already exists.")
        servers.append(record)
        self._save(servers)
        return {**record, "scope": self.scope}

    def update(self, server_id: str, changes: Any) -> Dict[str, Any]:
        servers = self.list()
        for index, item in enumerate(servers):
            if item["id"] == server_id:
                record = clean_server(changes, existing={k: v for k, v in item.items() if k != "scope"})
                servers[index] = record
                self._save(servers)
                return {**record, "scope": self.scope}
        raise McpError("There is no MCP server with that id.")

    def remove(self, server_id: str) -> Dict[str, Any]:
        servers = self.list()
        kept = [s for s in servers if s["id"] != server_id]
        if len(kept) == len(servers):
            raise McpError("There is no MCP server with that id.")
        self._save(kept)
        return next(s for s in servers if s["id"] == server_id)


class BotMcpStore:
    """A bot's own servers, kept on its record (`mcp_servers`), offered to that
    bot only: a command run inside the bot's computer (transport computer), or
    a URL the API connects to (http or sse) with the bot's secrets."""

    scope = "bot"
    MAX = 8
    TRANSPORTS = ("computer", "http", "sse")

    @classmethod
    def _transport(cls, data: Any, existing: Optional[Dict[str, Any]] = None) -> str:
        """A URL server stays one; anything else is a command in the computer."""
        asked = str((data or {}).get("transport") or (existing or {}).get("transport") or "").strip().lower()
        if asked in ("http", "sse"):
            return asked
        if not asked and (data or {}).get("url") and not (data or {}).get("command"):
            return "http"
        return "computer"

    def __init__(self, bot_id: str, storage: Optional[StorageService] = None):
        self.bot_id = bot_id
        self._storage = storage

    @property
    def storage(self) -> StorageService:
        return self._storage or current_storage()

    def _bots(self) -> List[Dict[str, Any]]:
        return self.storage.get_bots()

    def _bot(self, bots: List[Dict[str, Any]]) -> Dict[str, Any]:
        for bot in bots:
            if bot.get("id") == self.bot_id:
                return bot
        raise McpError("There is no such bot.")

    def list(self) -> List[Dict[str, Any]]:
        try:
            raw = self._bot(self._bots()).get("mcp_servers") or []
        except McpError:
            return []
        servers = []
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict) and _ID.fullmatch(str(item.get("id") or "")):
                transport = item.get("transport") if item.get("transport") in self.TRANSPORTS else "computer"
                servers.append({**item, "scope": "bot", "bot_id": self.bot_id, "transport": transport})
        return servers

    def get(self, server_id: str) -> Dict[str, Any]:
        for item in self.list():
            if item["id"] == server_id:
                return item
        raise McpError("There is no MCP server with that id.")

    def _save(self, servers: List[Dict[str, Any]]) -> None:
        bots = self._bots()
        bot = self._bot(bots)
        bot["mcp_servers"] = [{k: v for k, v in s.items() if k not in ("scope", "bot_id")} for s in servers]
        self.storage.save_bots(bots)

    def add(self, data: Any, *, created_by: str = "user") -> Dict[str, Any]:
        servers = self.list()
        if len(servers) >= self.MAX:
            raise McpError(f"At most {self.MAX} MCP servers per bot.")
        data = data if isinstance(data, dict) else {}
        record = clean_server({**data, "transport": self._transport(data), "created_by": created_by})
        if any(s["name"].lower() == record["name"].lower() for s in servers):
            raise McpError(f"A server named {record['name']} already exists on this bot.")
        servers.append(record)
        self._save(servers)
        return {**record, "scope": "bot", "bot_id": self.bot_id}

    def update(self, server_id: str, changes: Any) -> Dict[str, Any]:
        servers = self.list()
        for index, item in enumerate(servers):
            if item["id"] == server_id:
                changes = changes if isinstance(changes, dict) else {}
                record = clean_server({**changes, "transport": self._transport(changes, item)}, existing={k: v for k, v in item.items() if k not in ("scope", "bot_id")})
                servers[index] = record
                self._save(servers)
                return {**record, "scope": "bot", "bot_id": self.bot_id}
        raise McpError("There is no MCP server with that id.")

    def remove(self, server_id: str) -> Dict[str, Any]:
        servers = self.list()
        kept = [s for s in servers if s["id"] != server_id]
        if len(kept) == len(servers):
            raise McpError("There is no MCP server with that id.")
        self._save(kept)
        return next(s for s in servers if s["id"] == server_id)


# ---- connections ------------------------------------------------------------


def _computer_running(record: Dict[str, Any]) -> bool:
    try:
        from app.services.computer_provider import computer_provider

        return computer_provider.describe(str(record.get("bot_id") or "")).state == "running"
    except Exception:
        return False


async def computer_endpoint(record: Dict[str, Any]) -> Dict[str, Any]:
    """Make sure the bot's computer runs and the server runs in it; the endpoint to talk to."""
    from app.services.computer_provider import ComputerProviderError, computer_provider

    bot_id = str(record.get("bot_id") or "")
    if not bot_id:
        raise McpError("A computer server needs the bot it belongs to.")
    try:
        status = computer_provider.get_or_create(bot_id)
        if status.state != "running":
            status = await computer_provider.start(status.computer_id)
        await computer_provider.mcp_start(status.computer_id, {"id": record["id"], "command": record.get("command"), "args": record.get("args") or [], "env": record.get("env") or {}})
        endpoint = computer_provider.mcp_endpoint(status.computer_id, record["id"])
    except ComputerProviderError as exc:
        raise McpError(f"The bot's computer could not run the server: {exc}") from exc
    if not endpoint:
        raise McpError("The bot's computer is not running.")
    return endpoint


@dataclass
class McpTool:
    name: str
    description: str
    input_schema: Dict[str, Any]
    read_only: Optional[bool]
    destructive: Optional[bool]
    title: Optional[str] = None


# What _unless_lost returns when the session is lost first.
_LOST = object()


@dataclass
class _Request:
    name: str
    arguments: Dict[str, Any]
    future: "asyncio.Future[Any]"


@dataclass
class McpConnection:
    """One long-lived session with a server, driven by a background task."""

    record: Dict[str, Any]
    secrets: Dict[str, str] = field(default_factory=dict)
    status: str = "stopped"  # stopped | connecting | connected | error
    error: str = ""
    tools: List[McpTool] = field(default_factory=list)
    server_name: str = ""
    connected_at: Optional[float] = None
    # The protocol the session runs over (a URL server's may be either).
    via: str = ""
    _task: Optional["asyncio.Task[None]"] = None
    _queue: "asyncio.Queue[Optional[_Request]]" = field(default_factory=asyncio.Queue)
    _ready: "asyncio.Event" = field(default_factory=asyncio.Event)
    # Set when a URL server's stream ends under the session.
    _lost: "asyncio.Event" = field(default_factory=asyncio.Event)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._ready = asyncio.Event()
        self._queue = asyncio.Queue()
        self.status = "connecting"
        self.error = ""
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def wait_ready(self, timeout: float = CONNECT_TIMEOUT) -> None:
        self.start()
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except asyncio.TimeoutError:
            raise McpError(f"{self.record['name']} did not connect within {int(timeout)} seconds.")
        if self.status != "connected":
            raise McpError(self.error or f"{self.record['name']} is not connected.")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        await self._queue.put(None)
        try:
            await asyncio.wait_for(task, 10)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        self._task = None
        self.status = "stopped"

    async def call(self, name: str, arguments: Dict[str, Any], timeout: float = CALL_TIMEOUT) -> Any:
        await self.wait_ready()
        future: "asyncio.Future[Any]" = asyncio.get_running_loop().create_future()
        await self._queue.put(_Request(name, arguments, future))
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise McpError(f"{self.record['name']} did not answer {name} within {int(timeout)} seconds.")

    def _protocols(self) -> List[str]:
        transport = self.record["transport"]
        if transport != "http":
            return [transport]
        from urllib.parse import urlparse

        protocols = ["sse", "http"] if _SSE_PATH.search(urlparse(self.record.get("url") or "").path) else ["http", "sse"]
        # The one that answered before goes first.
        return sorted(protocols, key=lambda protocol: protocol != self.via)

    async def _run(self) -> None:
        try:
            away: Optional[int] = None  # attempts since the session was lost
            while True:
                if self._ready.is_set():
                    self._ready = asyncio.Event()  # whoever waits now waits for this attempt
                self.status = "connecting"
                try:
                    if not await self._connect():
                        return
                    # The server restarted or went away under the session: connect again.
                    logger.info("MCP server %s: the connection was lost; reconnecting", self.record["name"])
                    away = 0
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    if away is None:
                        raise
                    # Still away: say so, and try again a little later.
                    self.status, self.error = "error", _first_message(exc)
                    self._ready.set()
                    await asyncio.sleep(RECONNECT_DELAYS[min(away, len(RECONNECT_DELAYS) - 1)])
                    away += 1
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # anyio raises groups; keep the first message.
            message = _first_message(exc)
            self.status = "error"
            self.error = message
            logger.warning("MCP server %s failed: %s", self.record["name"], message)
        finally:
            if self.status != "error":
                self.status = "stopped"
            self._ready.set()
            # Nobody waits on a dead session.
            while not self._queue.empty():
                pending = self._queue.get_nowait()
                if pending is not None and not pending.future.done():
                    pending.future.set_exception(McpError(self.error or "The MCP server stopped."))

    async def _connect(self) -> bool:
        """Connect over the first protocol that answers and serve calls; True when the session was lost."""
        protocols = self._protocols()
        for index, protocol in enumerate(protocols):
            try:
                return await self._serve(protocol)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                # Only a failure to connect moves on to the other protocol.
                if self.status == "connected" or index == len(protocols) - 1:
                    raise
                logger.info("MCP server %s did not connect over %s (%s); trying %s", self.record["name"], protocol, _first_message(exc), protocols[index + 1])
        return False

    async def _serve(self, protocol: str) -> bool:
        """One session over one protocol, serving calls until stopped (False) or lost (True)."""
        self._lost = asyncio.Event()
        async with self._session(protocol) as session:
            init = await session.initialize()
            info = getattr(init, "server_info", None)
            self.server_name = str(getattr(info, "name", "") or "")
            self.tools = await self._list_tools(session)
            self.status, self.error = "connected", ""
            self.connected_at = time.time()
            self._ready.set()
            logger.info("MCP server %s connected over %s: %d tools", self.record["name"], protocol, len(self.tools))
            while True:
                request = await self._unless_lost(self._queue.get())
                if request is _LOST:
                    return True
                if request is None:
                    return False
                try:
                    result = await self._unless_lost(session.call_tool(request.name, request.arguments))
                    if result is _LOST:
                        if not request.future.done():
                            request.future.set_exception(McpError(f"The connection to {self.record['name']} was lost (the server restarted or went away); it is reconnecting, try again in a moment."))
                        return True
                    if not request.future.done():
                        request.future.set_result(result)
                except Exception as exc:  # One failed call must not end the session.
                    if not request.future.done():
                        request.future.set_exception(McpError(str(exc) or exc.__class__.__name__))
                    if self.record.get("transport") == "computer":
                        # The computer restarted or the server died: the
                        # next turn starts a fresh session (and server).
                        raise
                    if self._lost.is_set():
                        return True

    async def _unless_lost(self, awaitable: Any) -> Any:
        """The awaitable's result, or _LOST if the session is lost first."""
        work = asyncio.ensure_future(awaitable)
        lost = asyncio.ensure_future(self._lost.wait())
        try:
            await asyncio.wait({work, lost}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            lost.cancel()
            if not work.done():
                work.cancel()
                with contextlib.suppress(BaseException):
                    await work
        if work.cancelled():
            return _LOST
        return work.result()

    @contextlib.asynccontextmanager
    async def _watched(self, streams: Any):
        """A URL server's messages passed on until its stream ends (a restart, a dropped
        connection): then the session is lost, and is connected again."""
        import anyio

        send, receive = anyio.create_memory_object_stream(0)

        async def forward() -> None:
            async with send:
                async for item in streams[0]:
                    await send.send(item)
            self._lost.set()

        async with anyio.create_task_group() as group:
            group.start_soon(forward)
            try:
                yield receive, streams[1]
            finally:
                group.cancel_scope.cancel()

    @contextlib.asynccontextmanager
    async def _session(self, protocol: str):
        from mcp import ClientSession

        record = self.record
        self.via = protocol
        if protocol == "sse":
            from mcp.client.sse import sse_client

            headers = resolve_values(record.get("headers") or {}, self.secrets)
            async with sse_client(record["url"], headers=headers, timeout=CONNECT_TIMEOUT, sse_read_timeout=SSE_READ_TIMEOUT) as streams:
                async with self._watched(streams) as watched:
                    async with ClientSession(watched[0], watched[1]) as session:
                        yield session
        elif record["transport"] == "stdio":
            from mcp.client.stdio import StdioServerParameters, stdio_client

            env = {name: os.environ[name] for name in _INHERITED_ENV if name in os.environ}
            env.update(resolve_values(record.get("env") or {}, self.secrets))
            params = StdioServerParameters(command=record["command"], args=list(record.get("args") or []), env=env)
            async with stdio_client(params) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    yield session
        else:
            import httpx
            from mcp.client.streamable_http import streamable_http_client

            if record["transport"] == "computer":
                # Started (or already running) inside the bot's computer; its
                # $NAME values resolve there, from the computer's environment.
                endpoint = await computer_endpoint(record)
                url, headers = endpoint["url"], dict(endpoint.get("headers") or {})
            else:
                url = record["url"]
                headers = resolve_values(record.get("headers") or {}, self.secrets)
            async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(CALL_TIMEOUT, connect=CONNECT_TIMEOUT)) as client:
                async with streamable_http_client(url, http_client=client) as streams:
                    # A computer server's session ending is the computer's business (see _serve).
                    async with (contextlib.nullcontext(streams) if record["transport"] == "computer" else self._watched(streams)) as watched:
                        async with ClientSession(watched[0], watched[1]) as session:
                            yield session

    @staticmethod
    async def _list_tools(session: Any) -> List[McpTool]:
        tools: List[McpTool] = []
        cursor = None
        for _ in range(20):
            params = None
            if cursor:
                from mcp.types import PaginatedRequestParams

                params = PaginatedRequestParams(cursor=cursor)
            listing = await session.list_tools(params=params)
            for tool in listing.tools:
                annotations = getattr(tool, "annotations", None)
                schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
                tools.append(
                    McpTool(
                        name=str(tool.name),
                        description=str(getattr(tool, "description", "") or ""),
                        input_schema=dict(schema) if isinstance(schema, dict) else {},
                        read_only=getattr(annotations, "read_only_hint", None) if annotations else None,
                        destructive=getattr(annotations, "destructive_hint", None) if annotations else None,
                        title=getattr(tool, "title", None),
                    )
                )
            cursor = getattr(listing, "next_cursor", None) or getattr(listing, "nextCursor", None)
            if not cursor:
                break
        return tools


def _first_message(exc: BaseException) -> str:
    seen = exc
    for _ in range(6):
        group = getattr(seen, "exceptions", None)
        if group:
            seen = group[0]
            continue
        break
    text = str(seen) or seen.__class__.__name__
    return text[:400]


def result_text(result: Any) -> Dict[str, Any]:
    """A tool result as the model sees it: text joined, structured data kept, errors flagged."""
    parts: List[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
        else:
            kind = getattr(item, "type", None) or item.__class__.__name__
            parts.append(f"[{kind} content omitted]")
    payload: Dict[str, Any] = {"text": "\n".join(parts)}
    structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if structured:
        payload["structured"] = structured
    payload["is_error"] = bool(getattr(result, "is_error", False) or getattr(result, "isError", False))
    return payload


class McpManager:
    """Connections for every configured server, by id."""

    def __init__(self, store: Optional[McpStore] = None, secrets: Optional[Any] = None, user_store: Optional[McpStore] = None):
        # `store` is the system scope (the name predates user scopes).
        self.store = store or McpStore(scope="system")
        self.user_store = user_store or McpStore(storage=store._storage if store is not None else None, scope="user")
        # Callable returning the app secrets, resolved at connect time.
        self._secrets = secrets
        self._connections: Dict[str, McpConnection] = {}

    def _secrets_for(self, record: Dict[str, Any]) -> Dict[str, str]:
        """$NAME references: a system server reads the first account's app
        secrets, a user server the secrets of the account that owns it, a bot's
        URL server those with the bot's own on top (a command in the bot's
        computer resolves them there instead)."""
        if self._secrets is not None:
            return dict(self._secrets())
        try:
            if record.get("scope") == "bot":
                return dict(current_storage().secret_values(str(record.get("bot_id") or "")))
            if record.get("scope") == "user":
                return dict(current_storage().secret_values(""))
            return dict(storage_service.for_owner(LOCAL_USER_ID).secret_values(""))
        except Exception:
            return {}

    def bot_store(self, bot_id: str) -> BotMcpStore:
        return BotMcpStore(bot_id, storage=self.store._storage)

    def records(self, bot_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """System servers, the current account's own, and (given a bot) that bot's."""
        # A teammate on a shared bot gets only the owner's own servers the owner shared with it.
        found = self.store.list() + (self.user_store.list() if acting_for_owner() else self._shared_user_records(bot_id))
        if bot_id:
            found += self.bot_store(bot_id).list()
        return found

    def _shared_user_records(self, bot_id: Optional[str]) -> List[Dict[str, Any]]:
        if not bot_id:
            return []
        from app.services import sharing

        try:
            bot = next((b for b in self.user_store.storage.get_bots() if b.get("id") == bot_id), None)
        except Exception:
            return []
        shared = set(sharing.share_settings(bot)["mcp_servers"])
        return [record for record in self.user_store.list() if record.get("id") in shared]

    def _all_bot_records(self) -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []
        try:
            bots = self.user_store.storage.get_bots()
        except Exception:
            return found
        for bot in bots:
            if bot.get("mcp_servers"):
                found += self.bot_store(str(bot.get("id"))).list()
        return found

    def find(self, server_id: str, bot_id: Optional[str] = None) -> Dict[str, Any]:
        for record in self.records(bot_id):
            if record["id"] == server_id:
                return record
        if bot_id is None:
            # A bot's server called by its tools: look across the account's bots.
            for record in self._all_bot_records():
                if record["id"] == server_id:
                    return record
        raise McpError("There is no MCP server with that id.")

    def store_for(self, record: Dict[str, Any]):
        if record.get("scope") == "bot":
            return self.bot_store(str(record.get("bot_id") or ""))
        return self.user_store if record.get("scope") == "user" else self.store

    def connection(self, server_id: str, bot_id: Optional[str] = None) -> McpConnection:
        record = self.find(server_id, bot_id)
        current = self._connections.get(server_id)
        if current is None or current.record != record:
            if current is not None:
                asyncio.get_running_loop().create_task(current.stop())
            current = McpConnection(record=record, secrets=self._secrets_for(record))
            self._connections[server_id] = current
        return current

    def start_all(self) -> None:
        """Connect every enabled server of every account in the background (app startup)."""
        for record in self.store.list():
            self._start_record(record)
        try:
            owners = storage_service.list_owner_ids()
        except Exception:
            owners = [LOCAL_USER_ID]
        for owner_id in owners:
            with owner_context(owner_id):
                for record in self.user_store.list():
                    self._start_record(record)

    def _start_record(self, record: Dict[str, Any]) -> None:
        if not record.get("enabled", True):
            return
        try:
            self.connection(record["id"]).start()
        except Exception as exc:
            logger.warning("MCP server %s could not start: %s", record.get("name"), exc)

    async def stop_all(self) -> None:
        for connection in list(self._connections.values()):
            await connection.stop()
        self._connections = {}

    async def forget(self, server_id: str) -> None:
        connection = self._connections.pop(server_id, None)
        if connection is not None:
            await connection.stop()

    def statuses(self) -> Dict[str, Dict[str, Any]]:
        out = {}
        for server_id, connection in self._connections.items():
            out[server_id] = {
                "status": connection.status,
                "error": connection.error,
                "tools": len(connection.tools),
                "offered": sum(1 for tool in connection.tools if offers(connection.record, tool.name)),
                "server_name": connection.server_name,
                "via": connection.via if connection.status == "connected" else "",
            }
        return out

    def cached_tools(self, bot_id: Optional[str] = None) -> List[Any]:
        """(record, tools) for every enabled server the current account may use
        (system, its own, and the bot's) that is connected; nothing blocks here."""
        found = []
        for record in self.records(bot_id):
            if not record.get("enabled", True):
                continue
            connection = self._connections.get(record["id"])
            if connection is None or connection.record != record or (
                record.get("transport") == "computer" and connection.status in ("error", "stopped") and _computer_running(record)
            ):
                # A bot's server that died with its computer comes back once
                # the computer runs again.
                try:
                    connection = self.connection(record["id"], bot_id)
                    connection.start()
                except Exception as exc:  # No running loop, or a bad record.
                    logger.debug("MCP server %s not started: %s", record.get("name"), exc)
                    continue
            if connection.status == "connected":
                found.append((record, list(connection.tools)))
        return found

    async def call_tool(self, server_id: str, name: str, arguments: Dict[str, Any], bot_id: Optional[str] = None) -> Dict[str, Any]:
        connection = self.connection(server_id, bot_id)
        if not connection.record.get("enabled", True):
            raise McpError(f"{connection.record['name']} is switched off.")
        result = await connection.call(name, arguments)
        return result_text(result)

    async def test(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Connect once, list the tools, and disconnect."""
        probe = McpConnection(record=record, secrets=self._secrets_for(record))
        probe.start()
        try:
            await probe.wait_ready()
            return {
                "ok": True,
                "server_name": probe.server_name,
                "tools": [{"name": t.name, "read_only": t.read_only, "description": t.description[:160]} for t in probe.tools],
            }
        except McpError as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            await probe.stop()

    async def all_tools(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Every tool the server lists, offered or not, each marked (for choosing which it offers):
        from its live session when it has one, otherwise from a test connection."""
        connection = self._connections.get(record["id"])
        if connection is not None and connection.status == "connected" and connection.record == record:
            listing = {"ok": True, "server_name": connection.server_name,
                       "tools": [{"name": t.name, "read_only": t.read_only, "description": t.description[:160]} for t in connection.tools]}
        else:
            listing = await self.test(record)
        for tool in listing.get("tools", []):
            tool["offered"] = offers(record, tool["name"])
        return listing


mcp_store = McpStore()
user_mcp_store = McpStore(scope="user")

mcp_manager = McpManager(mcp_store, user_store=user_mcp_store)