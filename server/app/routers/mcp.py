"""MCP servers: list, add, change, remove, test, reconnect."""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

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


def _entry(record: Dict[str, Any]) -> Dict[str, Any]:
    status = mcp_manager.statuses().get(record["id"], {})
    entry = public(record)
    entry.update(
        {
            "status": status.get("status") or ("stopped" if entry["enabled"] else "disabled"),
            "error": status.get("error") or "",
            "tool_count": status.get("tools", 0),
            "server_name": status.get("server_name") or "",
        }
    )
    return entry


@router.get("/servers")
async def mcp_list_servers():
    return {"servers": [_entry(r) for r in mcp_store.list()]}


@router.post("/servers")
async def mcp_add_server(body: ServerInput):
    record = _guard(lambda: mcp_store.add(body.model_dump(exclude_none=True), created_by="user"))
    if record.get("enabled", True):
        mcp_manager.connection(record["id"]).start()
    return {"server": _entry(record)}


@router.put("/servers/{server_id}")
async def mcp_update_server(server_id: str, body: ServerUpdate):
    record = _guard(lambda: mcp_store.update(server_id, body.model_dump(exclude_none=True)))
    await mcp_manager.forget(server_id)
    if record.get("enabled", True):
        mcp_manager.connection(server_id).start()
    return {"server": _entry(record)}


@router.delete("/servers/{server_id}")
async def mcp_remove_server(server_id: str):
    record = _guard(lambda: mcp_store.remove(server_id))
    await mcp_manager.forget(server_id)
    return {"status": "ok", "removed": public(record)}


@router.post("/servers/{server_id}/test")
async def mcp_test_server(server_id: str):
    record = _guard(lambda: mcp_store.get(server_id))
    return await mcp_manager.test(record)


@router.post("/servers/{server_id}/reconnect")
async def mcp_reconnect_server(server_id: str):
    record = _guard(lambda: mcp_store.get(server_id))
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

    record = _guard(lambda: clean_server(body.model_dump(exclude_none=True)))
    return await mcp_manager.test(record)
