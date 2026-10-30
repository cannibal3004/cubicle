"""MCP servers: list, add, change, remove, test, reconnect."""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services.identity import is_admin
from app.services.mcp_service import McpError, mcp_manager, mcp_store, public

router = APIRouter(prefix="/api/v1/mcp", tags=["mcp"])


class ServerInput(BaseModel):
    name: str = Field(max_length=60)
    transport: Optional[str] = None
    command: Optional[str] = Field(default=None, max_length=300)
    args: Optional[Any] = None
    env: Optional[Dict[str, str]] = None
    url: Optional[str] = Field(default=None, max_length=500)
    headers: Optional[Dict[str, str]] = None
    enabled: Optional[bool] = None
    trust_read_only: Optional[bool] = None
    # system: every bot of every account (admins only); user: the caller's bots.
    scope: str = "user"


class ServerUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=60)
    transport: Optional[str] = None
    command: Optional[str] = Field(default=None, max_length=300)
    args: Optional[Any] = None
    env: Optional[Dict[str, str]] = None
    url: Optional[str] = Field(default=None, max_length=500)
    headers: Optional[Dict[str, str]] = None
    enabled: Optional[bool] = None
    trust_read_only: Optional[bool] = None


def _guard(action):
    try:
        return action()
    except McpError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _store(request: Request, scope: str):
    if scope == "system":
        if not is_admin(getattr(request.state, "user", None)):
            raise HTTPException(status_code=403, detail="Only an admin can change system MCP servers.")
        return mcp_manager.store
    return mcp_manager.user_store


def _record(request: Request, server_id: str) -> Dict[str, Any]:
    """A server the caller may manage: their own, or a system one if admin."""
    record = _guard(lambda: mcp_manager.find(server_id))
    _store(request, record.get("scope", "system"))
    return record


def _entry(record: Dict[str, Any]) -> Dict[str, Any]:
    status = mcp_manager.statuses().get(record["id"], {})
    entry = public(record)
    entry["scope"] = record.get("scope", "system")
    entry.update(
        {
            "status": status.get("status") or ("stopped" if entry["enabled"] else "disabled"),
            "error": status.get("error") or "",
            "tool_count": status.get("tools", 0),
            "server_name": status.get("server_name") or "",
            # A URL server's protocol once connected: http (streamable) or sse.
            "via": status.get("via") or "",
        }
    )
    return entry


@router.get("/servers")
async def mcp_list_servers():
    """System servers and the caller's own, each with its scope."""
    return {"servers": [_entry(r) for r in mcp_manager.records()]}


@router.post("/servers")
async def mcp_add_server(body: ServerInput, request: Request):
    store = _store(request, body.scope)
    record = _guard(lambda: store.add(body.model_dump(exclude_none=True, exclude={"scope"}), created_by="user"))
    if record.get("enabled", True):
        mcp_manager.connection(record["id"]).start()
    return {"server": _entry(record)}


@router.put("/servers/{server_id}")
async def mcp_update_server(server_id: str, body: ServerUpdate, request: Request):
    store = mcp_manager.store_for(_record(request, server_id))
    record = _guard(lambda: store.update(server_id, body.model_dump(exclude_none=True)))
    await mcp_manager.forget(server_id)
    if record.get("enabled", True):
        mcp_manager.connection(server_id).start()
    return {"server": _entry(record)}


@router.delete("/servers/{server_id}")
async def mcp_remove_server(server_id: str, request: Request):
    store = mcp_manager.store_for(_record(request, server_id))
    record = _guard(lambda: store.remove(server_id))
    await mcp_manager.forget(server_id)
    return {"status": "ok", "removed": public(record)}


@router.post("/servers/{server_id}/test")
async def mcp_test_server(server_id: str, request: Request):
    record = _record(request, server_id)
    return await mcp_manager.test(record)


@router.post("/servers/{server_id}/reconnect")
async def mcp_reconnect_server(server_id: str, request: Request):
    record = _record(request, server_id)
    await mcp_manager.forget(server_id)
    connection = mcp_manager.connection(server_id)
    try:
        await connection.wait_ready()
    except McpError as exc:
        return {"server": _entry(record), "error": str(exc)}
    return {"server": _entry(record)}


@router.post("/test")
async def mcp_test_definition(body: ServerInput):
    """Try a definition before saving it."""
    from app.services.mcp_service import clean_server

    record = _guard(lambda: clean_server(body.model_dump(exclude_none=True, exclude={"scope"})))
    return await mcp_manager.test(record)


# ---- a bot's own servers (run inside its computer) -------------------------------------------

from app.services.storage_service import current_storage  # noqa: E402


def _owned_bot(bot_id: str) -> None:
    if not any(bot.get("id") == bot_id for bot in current_storage().get_bots()):
        raise HTTPException(status_code=404, detail="Bot not found")


@router.get("/bots/{bot_id}/servers")
async def bot_mcp_list(bot_id: str):
    _owned_bot(bot_id)
    return {"servers": [_entry(r) for r in mcp_manager.bot_store(bot_id).list()]}


@router.post("/bots/{bot_id}/servers")
async def bot_mcp_add(bot_id: str, body: ServerInput):
    _owned_bot(bot_id)
    store = mcp_manager.bot_store(bot_id)
    record = _guard(lambda: store.add(body.model_dump(exclude_none=True, exclude={"scope"}), created_by="user"))
    if record.get("enabled", True):
        mcp_manager.connection(record["id"], bot_id).start()
    return {"server": _entry(record)}


@router.put("/bots/{bot_id}/servers/{server_id}")
async def bot_mcp_update(bot_id: str, server_id: str, body: ServerUpdate):
    _owned_bot(bot_id)
    store = mcp_manager.bot_store(bot_id)
    record = _guard(lambda: store.update(server_id, body.model_dump(exclude_none=True)))
    await mcp_manager.forget(server_id)
    if record.get("enabled", True):
        mcp_manager.connection(server_id, bot_id).start()
    return {"server": _entry(record)}


@router.delete("/bots/{bot_id}/servers/{server_id}")
async def bot_mcp_remove(bot_id: str, server_id: str):
    _owned_bot(bot_id)
    record = _guard(lambda: mcp_manager.bot_store(bot_id).remove(server_id))
    await mcp_manager.forget(server_id)
    try:
        from app.services.computer_provider import computer_provider

        await computer_provider.mcp_stop(computer_provider.describe(bot_id).computer_id, server_id)
    except Exception:
        pass  # the computer may not be running; nothing to stop then
    return {"status": "ok", "removed": public(record)}


@router.post("/bots/{bot_id}/servers/{server_id}/test")
async def bot_mcp_test(bot_id: str, server_id: str):
    _owned_bot(bot_id)
    record = _guard(lambda: mcp_manager.bot_store(bot_id).get(server_id))
    return await mcp_manager.test(record)

