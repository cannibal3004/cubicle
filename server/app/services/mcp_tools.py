"""MCP servers as model tools, and the tools a bot uses to manage servers.

Each tool of a connected server becomes a ToolSpec named <server>_<tool>,
in the connector family (so the per-bot and app-wide toolkit switches,
the catalog and the prompt treat a server like a connected app). The
protocol's annotations decide approval: a tool marked read-only runs
directly; anything else asks, unless the server is trusted as read-only
in its settings. Results are the server's text content.

The management tools let a bot set a server up when asked: list, add,
test and remove. Adding and removing pause for approval; secret values
never pass through the model, only `$NAME` references to app secrets.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from app.services.action_gateway import ActionDefinition, ActionInvocation, action_gateway
from app.services.connector_tools import sanitize_schema
from app.services.llm_tools import ToolSpec
from app.services.mcp_service import McpError, mcp_manager, mcp_store, offers, public  # noqa: F401 - mcp_store kept for tests

logger = logging.getLogger(__name__)

READ_ACTION = "mcp.read"
CALL_ACTION = "mcp.call"
_NAME_CHARS = re.compile(r"[^a-z0-9_]+")
# More than this many tools offered from one server, with no choice made, earns a warning in its settings.
MANY_TOOLS = 60


def toolkit_slug(server_id: str) -> str:
    return f"mcp:{server_id}"


def _slug(text: str) -> str:
    return _NAME_CHARS.sub("_", text.lower()).strip("_") or "tool"


def spec_for(record: Dict[str, Any], tool: Any, taken: set) -> ToolSpec:
    server_part = _slug(record["name"])[:24]
    name = f"{server_part}_{_slug(tool.name)}"[:64].rstrip("_")
    base, n = name, 2
    while name in taken:
        name = f"{base[:60]}_{n}"
        n += 1
    read_only = bool(tool.read_only) or bool(record.get("trust_read_only"))
    description = f"[{record['name']}] {tool.description or tool.title or tool.name}"[:1024]
    return ToolSpec(
        name=name,
        description=description,
        parameters=sanitize_schema(tool.input_schema),
        group="connector",
        needs_approval=not read_only,
        connector={
            "slug": tool.name,
            "toolkit": toolkit_slug(record["id"]),
            "toolkit_name": record["name"],
            "title": tool.title or tool.name,
            "read_only": read_only,
            "mcp": {"server": record["id"], "tool": tool.name},
        },
    )


def mcp_tool_specs(manager: Any = None, bot_id: Optional[str] = None) -> List[ToolSpec]:
    """Specs for every tool of every connected, enabled server the bot may use; nothing blocks."""
    manager = manager if manager is not None else mcp_manager
    specs: List[ToolSpec] = []
    taken: set = set()
    try:
        servers = manager.cached_tools(bot_id)
    except Exception as exc:
        logger.warning("MCP tools unavailable: %s", exc)
        return []
    for record, tools in servers:
        for tool in offered_tools(record, tools):
            spec = spec_for(record, tool, taken)
            taken.add(spec.name)
            specs.append(spec)
    return specs


def offered_tools(record: Dict[str, Any], tools: List[Any]) -> List[Any]:
    """The server's tools its tool filter lets through (a server may list hundreds)."""
    return [tool for tool in tools if offers(record, tool.name)]


# ---- executing a server's tool ---------------------------------------------------


async def execute_mcp_tool(call: ActionInvocation) -> Dict[str, Any]:
    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    server_id = arguments.get("server")
    tool = arguments.get("tool")
    payload = arguments.get("arguments")
    if not isinstance(server_id, str) or not isinstance(tool, str) or not isinstance(payload, dict):
        raise McpError("The MCP tool call is malformed.")
    result = await mcp_manager.call_tool(server_id, tool, payload, bot_id=_bot_of(call))
    if result.get("is_error"):
        raise McpError(result.get("text") or f"{tool} reported an error.")
    return {"server": server_id, "tool": tool, **result}


# ---- managing servers (the bot's side) ---------------------------------------------


def _server_summary(record: Dict[str, Any]) -> Dict[str, Any]:
    status = mcp_manager.statuses().get(record["id"], {})
    entry = public(record)
    entry.update({"status": status.get("status") or ("stopped" if entry["enabled"] else "disabled"), "error": status.get("error") or "", "tool_count": status.get("tools", 0), "offered_count": status.get("offered", 0)})
    return entry


def _bot_of(call: ActionInvocation) -> Optional[str]:
    target = call.target if isinstance(call.target, dict) else {}
    bot_id = target.get("bot_id")
    return str(bot_id) if bot_id else None


async def list_servers(call: ActionInvocation) -> Dict[str, Any]:
    return {"servers": [_server_summary(r) for r in mcp_manager.records(_bot_of(call))]}


async def add_server(call: ActionInvocation) -> Dict[str, Any]:
    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    bot_id = _bot_of(call)
    # scope "computer": the server runs inside this bot's computer and only this bot gets it.
    store = mcp_manager.bot_store(bot_id) if arguments.get("scope") == "computer" and bot_id else mcp_manager.user_store
    record = store.add(arguments.get("server"), created_by="bot")
    test = await mcp_manager.test(record)
    if not test.get("ok"):
        # Keep it, switched off, so the user can fix it in the editor or settings.
        record = store.update(record["id"], {"enabled": False})
        return {"server": _server_summary(record), "connected": False, "error": test.get("error"), "note": "Saved but switched off: it did not connect. Tell the user what failed."}
    mcp_manager.connection(record["id"], bot_id).start()
    tools = [t for t in test.get("tools", []) if offers(record, t["name"])]
    note = "Its tools are offered from your next turn."
    if record.get("tool_mode") == "only":
        missing = [n for n in record.get("tool_names") or [] if n not in {t["name"] for t in test.get("tools", [])}]
        note += f" It offers {len(tools)} of its {len(test.get('tools', []))} tools." + (f" Not among them: {', '.join(missing[:20])}." if missing else "")
    return {"server": _server_summary(record), "connected": True, "tools": tools, "note": note}


async def test_server(call: ActionInvocation) -> Dict[str, Any]:
    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    record = mcp_manager.find(str(arguments.get("id") or ""), _bot_of(call))
    return {"server": _server_summary(record), **(await mcp_manager.all_tools(record))}


async def remove_server(call: ActionInvocation) -> Dict[str, Any]:
    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    server_id = str(arguments.get("id") or "")
    record = mcp_manager.find(server_id, _bot_of(call))
    if record.get("scope") == "system":
        raise McpError("System servers are managed by an admin in Settings.")
    record = mcp_manager.store_for(record).remove(server_id)
    await mcp_manager.forget(server_id)
    return {"removed": public(record)}


def register_mcp_actions(gateway: Any = None) -> None:
    target = gateway or action_gateway
    target.register_action(
        ActionDefinition(name=READ_ACTION, tool="mcp", action="read", intent="Read through an MCP server.", risk="read", requires_approval=False),
        execute_mcp_tool,
    )
    target.register_action(
        ActionDefinition(name=CALL_ACTION, tool="mcp", action="call", intent="Act through an MCP server.", risk="external", requires_approval=True),
        execute_mcp_tool,
    )
    target.register_action(
        ActionDefinition(name="mcp.servers_list", tool="mcp", action="servers_list", intent="Show the configured MCP servers.", risk="read", requires_approval=False),
        list_servers,
    )
    target.register_action(
        ActionDefinition(name="mcp.servers_add", tool="mcp", action="servers_add", intent="Add an MCP server.", risk="write", requires_approval=True),
        add_server,
    )
    target.register_action(
        ActionDefinition(name="mcp.servers_test", tool="mcp", action="servers_test", intent="Connect to an MCP server and list its tools.", risk="read", requires_approval=False),
        test_server,
    )
    target.register_action(
        ActionDefinition(name="mcp.servers_remove", tool="mcp", action="servers_remove", intent="Remove an MCP server.", risk="write", requires_approval=True),
        remove_server,
    )


register_mcp_actions()
