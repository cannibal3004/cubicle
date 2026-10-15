"""MCP servers: local or remote tool servers the bots can use.

A server is configured once (App Settings, or by a bot through the mcp_*
tools after approval) and kept in the settings store under `mcp_servers`:
a name, a transport (a command to run over stdio, or a streamable-HTTP
URL), environment variables or headers, and whether its tools may be
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
from app.services.storage_service import StorageService, storage_service

logger = logging.getLogger(__name__)

TRANSPORTS = ("stdio", "http")
MAX_SERVERS = 30
MAX_NAME_CHARS = 60
MAX_ARGS = 40
MAX_ENV = 30
_SECRET_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]{0,63})\}|\$([A-Z][A-Z0-9_]{0,63})\b")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_ID = re.compile(r"^mcp-[a-f0-9]{8}$")
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
        raise McpError("'transport' must be stdio (a command) or http (a URL).")
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
        "created_by": base.get("created_by") or (str(merged.get("created_by") or "user")),
        "created_at": base.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if transport == "stdio":
        command = str(merged.get("command") or "").strip()
        if not command:
            raise McpError("A stdio server needs the command to run, e.g. npx.")
        if len(command) > 300 or any(ch in command for ch in "\n\r\0"):
            raise McpError("The command looks wrong.")
        record["command"] = command
        record["args"] = _clean_args(merged.get("args"))
        record["env"] = _clean_map(merged.get("env"), "env")
    else:
        url = str(merged.get("url") or "").strip()
        if not re.match(r"^https?://[^\s]+$", url) or len(url) > 500:
            raise McpError("An http server needs an http(s) URL.")
        record["url"] = url
        record["headers"] = _clean_map(merged.get("headers"), "headers")
    return record


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
                raise McpError(f"The app secret {name} is not set; add it in App Settings -> Secrets.")

        def _sub(match: "re.Match[str]") -> str:
            return secrets[match.group(1) or match.group(2)]

        resolved[key] = _SECRET_REF.sub(_sub, text)
    return resolved


def public(record: Dict[str, Any]) -> Dict[str, Any]:
    """The record as the API and the model see it: secret references stay references."""
    return {
        "id": record["id"],
        "name": record["name"],
        "transport": record["transport"],
        "command": record.get("command") or "",
        "args": list(record.get("args") or []),
        "env": dict(record.get("env") or {}),
        "url": record.get("url") or "",
        "headers": dict(record.get("headers") or {}),
        "enabled": bool(record.get("enabled", True)),
        "trust_read_only": bool(record.get("trust_read_only", False)),
        "created_by": record.get("created_by") or "user",
        "created_at": record.get("created_at"),
        "secret_references": secret_references(record),
    }


class McpStore:
    """The configured servers, kept in app settings under `mcp_servers`."""

    def __init__(self, storage: Optional[StorageService] = None):
        self._storage = storage

    @property
    def storage(self) -> StorageService:
        return self._storage or storage_service

    def list(self) -> List[Dict[str, Any]]:
        raw = self.storage.get_settings().get("mcp_servers") or []
        servers = []
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict) and _ID.fullmatch(str(item.get("id") or "")):
                servers.append(item)
        return servers

    def get(self, server_id: str) -> Dict[str, Any]:
        for item in self.list():
            if item["id"] == server_id:
                return item
        raise McpError("There is no MCP server with that id.")

    def _save(self, servers: List[Dict[str, Any]]) -> None:
        self.storage.save_settings({"mcp_servers": servers})

    def add(self, data: Any, *, created_by: str = "user") -> Dict[str, Any]:
        servers = self.list()
        if len(servers) >= MAX_SERVERS:
            raise McpError(f"At most {MAX_SERVERS} MCP servers.")
        record = clean_server({**(data if isinstance(data, dict) else {}), "created_by": created_by})
        if any(s["name"].lower() == record["name"].lower() for s in servers):
            raise McpError(f"A server named {record['name']} already exists.")
        servers.append(record)
        self._save(servers)
        return record

    def update(self, server_id: str, changes: Any) -> Dict[str, Any]:
        servers = self.list()
        for index, item in enumerate(servers):
            if item["id"] == server_id:
                record = clean_server(changes, existing=item)
                servers[index] = record
                self._save(servers)
                return record
        raise McpError("There is no MCP server with that id.")

    def remove(self, server_id: str) -> Dict[str, Any]:
        servers = self.list()
        kept = [s for s in servers if s["id"] != server_id]
        if len(kept) == len(servers):
            raise McpError("There is no MCP server with that id.")
        self._save(kept)
        return next(s for s in servers if s["id"] == server_id)


# ---- connections ------------------------------------------------------------


@dataclass
class McpTool:
    name: str
    description: str
    input_schema: Dict[str, Any]
    read_only: Optional[bool]
    destructive: Optional[bool]
    title: Optional[str] = None


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
    _task: Optional["asyncio.Task[None]"] = None
    _queue: "asyncio.Queue[Optional[_Request]]" = field(default_factory=asyncio.Queue)
    _ready: "asyncio.Event" = field(default_factory=asyncio.Event)

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

    async def _run(self) -> None:
        try:
            async with self._session() as session:
                init = await session.initialize()
                info = getattr(init, "server_info", None)
                self.server_name = str(getattr(info, "name", "") or "")
                self.tools = await self._list_tools(session)
                self.status = "connected"
                self.connected_at = time.time()
                self._ready.set()
                logger.info("MCP server %s connected: %d tools", self.record["name"], len(self.tools))
                while True:
                    request = await self._queue.get()
                    if request is None:
                        return
                    try:
                        result = await session.call_tool(request.name, request.arguments)
                        if not request.future.done():
                            request.future.set_result(result)
                    except Exception as exc:  # One failed call must not end the session.
                        if not request.future.done():
                            request.future.set_exception(McpError(str(exc) or exc.__class__.__name__))
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

    @contextlib.asynccontextmanager
    async def _session(self):
        from mcp import ClientSession

        record = self.record
        if record["transport"] == "stdio":
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

            headers = resolve_values(record.get("headers") or {}, self.secrets)
            async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(CALL_TIMEOUT, connect=CONNECT_TIMEOUT)) as client:
                async with streamable_http_client(record["url"], http_client=client) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
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

    def __init__(self, store: Optional[McpStore] = None, secrets: Optional[Any] = None):
        self.store = store or McpStore()
        # Callable returning the app secrets, resolved at connect time.
        self._secrets = secrets
        self._connections: Dict[str, McpConnection] = {}

    def _app_secrets(self) -> Dict[str, str]:
        if self._secrets is not None:
            return dict(self._secrets())
        try:
            return dict(self.store.storage.secret_values(""))
        except Exception:
            return {}

    def connection(self, server_id: str) -> McpConnection:
        record = self.store.get(server_id)
        current = self._connections.get(server_id)
        if current is None or current.record != record:
            if current is not None:
                asyncio.get_running_loop().create_task(current.stop())
            current = McpConnection(record=record, secrets=self._app_secrets())
            self._connections[server_id] = current
        return current

    def start_all(self) -> None:
        """Connect every enabled server in the background (app startup)."""
        for record in self.store.list():
            if record.get("enabled", True):
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
                "server_name": connection.server_name,
            }
        return out

    def cached_tools(self) -> List[Any]:
        """(record, tools) for every enabled server that is connected; nothing blocks here."""
        found = []
        for record in self.store.list():
            if not record.get("enabled", True):
                continue
            connection = self._connections.get(record["id"])
            if connection is None or connection.record != record:
                try:
                    connection = self.connection(record["id"])
                    connection.start()
                except Exception as exc:  # No running loop, or a bad record.
                    logger.debug("MCP server %s not started: %s", record.get("name"), exc)
                    continue
            if connection.status == "connected":
                found.append((record, list(connection.tools)))
        return found

    async def call_tool(self, server_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        connection = self.connection(server_id)
        if not connection.record.get("enabled", True):
            raise McpError(f"{connection.record['name']} is switched off.")
        result = await connection.call(name, arguments)
        return result_text(result)

    async def test(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Connect once, list the tools, and disconnect."""
        probe = McpConnection(record=record, secrets=self._app_secrets())
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


mcp_store = McpStore()
mcp_manager = McpManager(mcp_store)
