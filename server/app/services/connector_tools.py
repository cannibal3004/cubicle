"""Model tools for every app connected through Composio.

Composio publishes a JSON-schema description of each tool in a toolkit. For
each toolkit the user has an ACTIVE connection to, this module turns those
descriptions into `ToolSpec`s the model can call. Execution still goes through
the action gateway: a tool Composio marks read-only runs without approval,
anything else pauses for the user, exactly like the hand-written tools.
"""

import contextlib
import copy
import logging
import re
from typing import Any, Dict, Iterator, List, Optional

from app.config import settings
from app.services.action_gateway import ActionDefinition, ActionInvocation, action_gateway
from app.services import sharing
from app.services.composio_service import ConnectorServiceError, as_account, composio_service
from app.services.identity import acting_for_owner, get_current_actor, get_current_owner
from app.services.llm_tools import ToolSpec

logger = logging.getLogger(__name__)

# OpenAI-compatible servers accept function names matching this pattern.
_FUNCTION_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_SCHEMA_NOISE = {"title", "examples", "example", "x-composio", "file_uploadable"}

READ_ACTION = "connector.composio_read"
WRITE_ACTION = "connector.composio_action"


def sanitize_schema(schema: Any) -> Dict[str, Any]:
    """Reduce Composio's parameter schema to what tool-calling servers accept.

    Keeps types, properties, required, enums, descriptions, and defaults; drops
    titles and examples, and removes a stray `properties` on non-object nodes
    (Composio emits `{"type": "string", "properties": {}}` for array items).
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}, "additionalProperties": False}

    def clean(node: Any) -> Any:
        if isinstance(node, list):
            return [clean(item) for item in node]
        if not isinstance(node, dict):
            return node
        out: Dict[str, Any] = {}
        node_type = node.get("type")
        for key, value in node.items():
            if key in _SCHEMA_NOISE:
                continue
            if key == "properties":
                if node_type not in (None, "object"):
                    continue
                out[key] = {name: clean(sub) for name, sub in (value or {}).items() if isinstance(sub, dict)}
            elif key == "items":
                out[key] = clean(value)
            elif key in {"anyOf", "oneOf", "allOf"}:
                out[key] = clean(value)
            else:
                out[key] = copy.deepcopy(value)
        if out.get("type") == "object" and "properties" not in out:
            out["properties"] = {}
        return out

    cleaned = clean(schema)
    cleaned["type"] = "object"
    cleaned.setdefault("properties", {})
    cleaned["additionalProperties"] = False
    required = [name for name in cleaned.get("required") or [] if name in cleaned["properties"]]
    if required:
        cleaned["required"] = required
    else:
        cleaned.pop("required", None)
    return cleaned


def spec_from_composio_tool(tool: Dict[str, Any]) -> Optional[ToolSpec]:
    """Build the model-facing spec for one Composio tool, or None to skip it."""
    slug = str(tool.get("slug") or "").strip()
    if not slug or tool.get("is_deprecated") or tool.get("deprecated") is True:
        return None
    name = slug.lower()
    if not _FUNCTION_NAME.fullmatch(name):
        return None
    toolkit = tool.get("toolkit") if isinstance(tool.get("toolkit"), dict) else {}
    toolkit_slug = str(toolkit.get("slug") or slug.split("_", 1)[0]).lower()
    toolkit_name = str(toolkit.get("name") or toolkit_slug)
    tags = {str(tag) for tag in tool.get("tags") or []}
    read_only = "readOnlyHint" in tags and "destructiveHint" not in tags
    description = str(tool.get("description") or tool.get("human_description") or tool.get("name") or slug).strip()
    description = f"[{toolkit_name}] {description}"[:1024]
    return ToolSpec(
        name=name,
        description=description,
        parameters=sanitize_schema(tool.get("input_parameters")),
        group="connector",
        needs_approval=not read_only,
        connector={
            "slug": slug,
            "toolkit": toolkit_slug,
            "toolkit_name": toolkit_name,
            "title": str(tool.get("name") or slug),
            "read_only": read_only,
            "version": str(tool.get("version") or "") or None,
        },
    )


# Toolkits whose catalog needs curating when the dashboard sets no allow list.
# Composio's own toolkit also carries remote sandboxes, agent runners and
# trigger management, none of which belong in a local-first bot.
BUILTIN_ALLOW_LISTS: Dict[str, List[str]] = {
    "composio": [
        "COMPOSIO_SEARCH_TOOLS",
        "COMPOSIO_RETRIEVE_TOOLKITS",
        "COMPOSIO_GET_TOOL_SCHEMAS",
        "COMPOSIO_GET_REQUIRED_PARAMETERS",
        "COMPOSIO_CHECK_ACTIVE_CONNECTIONS",
        "COMPOSIO_EXECUTE_TOOL",
    ],
}


async def connector_tool_specs(service: Any = None) -> List[ToolSpec]:
    """Specs for every tool in every connected toolkit, or [] when unavailable.

    A missing key, an unreachable Composio, or a scoped key simply means the
    model is offered no connector tools this turn; the turn itself proceeds.
    """
    service = service if service is not None else composio_service
    getter = getattr(service, "get_api_key", None)
    if not callable(getter) or not getter():
        return []
    if not hasattr(service, "list_connected_toolkits") or not hasattr(service, "list_tools"):
        return []
    try:
        toolkits = await service.list_connected_toolkits()
    except ConnectorServiceError as exc:
        logger.warning("Connector tools unavailable: %s", exc)
        return []

    specs: List[ToolSpec] = []
    seen = set()
    per_toolkit = max(1, settings.COMPOSIO_TOOLS_PER_TOOLKIT)
    for toolkit in sorted(toolkits):
        try:
            # An allow list set in the Composio dashboard wins over the
            # "important" heuristic: offer exactly those tools, whether or
            # not Composio considers them important.
            allowed = None
            if hasattr(service, "allowed_tools"):
                allowed = await service.allowed_tools(toolkit)
            if allowed is None and toolkit in BUILTIN_ALLOW_LISTS:
                allowed = list(BUILTIN_ALLOW_LISTS[toolkit])
            if allowed is not None:
                tools = await service.list_tools(toolkit, important_only=False, limit=200, tool_slugs=list(allowed))
                wanted = set(allowed)
                tools = [tool for tool in tools if tool.get("slug") in wanted]
                missing = sorted(wanted - {tool.get("slug") for tool in tools})
                if missing:
                    logger.info("Allowed %s tools not in Composio's catalog: %s", toolkit, ", ".join(missing))
            else:
                tools = await service.list_tools(
                    toolkit,
                    important_only=settings.COMPOSIO_IMPORTANT_TOOLS_ONLY,
                    limit=per_toolkit,
                )
                if not tools and settings.COMPOSIO_IMPORTANT_TOOLS_ONLY:
                    # Small toolkits (MCP servers especially) have nothing
                    # flagged important; offer whatever they have instead.
                    tools = await service.list_tools(toolkit, important_only=False, limit=per_toolkit)
        except ConnectorServiceError as exc:
            logger.warning("Could not list %s tools: %s", toolkit, exc)
            continue
        count = 0
        for tool in tools:
            spec = spec_from_composio_tool(tool)
            if spec is None or spec.name in seen:
                continue
            seen.add(spec.name)
            specs.append(spec)
            count += 1
            if count >= per_toolkit:
                break
    return specs


def _bot(call: ActionInvocation) -> Optional[Dict[str, Any]]:
    from app.services.storage_service import current_storage

    bot_id = (call.target or {}).get("bot_id")
    return next((b for b in current_storage().get_bots() if b.get("id") == bot_id), None) if bot_id else None


@contextlib.contextmanager
def _connection(call: ActionInvocation, toolkit: str) -> Iterator[None]:
    """Run inside the account whose connection this call uses; on a teammate's
    turn that is the owner's or their own, as the bot's share settings say."""
    if acting_for_owner():
        yield
        return
    account = sharing.connection_for(_bot(call), toolkit)
    if account is None:
        raise ConnectorServiceError(f"{toolkit} is not shared with this bot's teammates.")
    with as_account(account):
        yield


def _toolkit_of(slug: str, call: ActionInvocation) -> str:
    return str((call.target or {}).get("connector") or slug.split("_", 1)[0]).lower()


async def execute_composio_tool(call: ActionInvocation) -> Dict[str, Any]:
    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    slug = arguments.get("tool")
    payload = arguments.get("arguments")
    version = arguments.get("version")
    if not isinstance(slug, str) or not isinstance(payload, dict):
        raise ConnectorServiceError("The connector tool call is malformed.")
    with _connection(call, _toolkit_of(slug, call)):
        result = await composio_service.execute_tool(slug, payload, version=version if isinstance(version, str) else None)
        if slug.upper().startswith("COMPOSIO_"):
            # Connections may have changed (the bot connected an app for the
            # user): the next turn lists toolkits afresh.
            composio_service.forget_connections()
    return result


async def shared_connector_specs(service: Any, bot: Optional[Dict[str, Any]]) -> List[ToolSpec]:
    """A teammate's connected-app tools on a shared bot: the apps the owner shared
    through their own connection, then the ones each person connects for themselves."""
    chosen = sharing.share_settings(bot)["apps"]
    theirs = {slug for slug, choice in chosen.items() if choice == sharing.OWNERS_CONNECTION}
    own = {slug for slug, choice in chosen.items() if choice == sharing.OWN_CONNECTION} | {sharing.COMPOSIO_TOOLKIT}
    specs: List[ToolSpec] = []
    if theirs:
        with as_account(get_current_owner()):
            specs += [s for s in await connector_tool_specs(service) if (s.connector or {}).get("toolkit") in theirs]
    with as_account(get_current_actor()):
        specs += [s for s in await connector_tool_specs(service) if (s.connector or {}).get("toolkit") in own]
    return specs


async def connect_app(call: ActionInvocation) -> Dict[str, Any]:
    """Start a connection the way the marketplace does; the model relays the link."""
    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    slug = str(arguments.get("toolkit") or "").strip().lower()
    if not slug:
        raise ConnectorServiceError("A toolkit slug is required.")
    if not acting_for_owner() and sharing.share_settings(_bot(call))["apps"].get(slug) != sharing.OWN_CONNECTION:
        raise ConnectorServiceError(f"{slug} is not one of the apps this bot uses with each person's own account, so connecting it here would not help.")
    with as_account(get_current_actor()):
        link = await composio_service.create_auth_link(slug)
        composio_service.forget_connections()
    if link.get("connected"):
        return {"toolkit": slug, "connected": True, "url": None, "note": "This app needs no sign-in; its tools are offered from your next turn."}
    return {
        "toolkit": slug,
        "connected": False,
        "url": link.get("url"),
        "expires_at": link.get("expires_at"),
        "note": "Give the user this link to sign in. Call connector_status once they say they have finished.",
    }


async def connection_status(call: ActionInvocation) -> Dict[str, Any]:
    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    slugs = [str(s).lower() for s in (arguments.get("toolkits") or []) if isinstance(s, str)]
    # A teammate's own connections (the owner's are theirs to look after).
    with as_account(get_current_actor()):
        composio_service.forget_connections()
        return {"services": await composio_service.connection_status(slugs)}


action_gateway.register_action(
    ActionDefinition(
        name="connector.connect",
        tool="connector",
        action="connect",
        intent="Start connecting an app through Composio for the user.",
        risk="external",
        requires_approval=True,
    ),
    connect_app,
)

action_gateway.register_action(
    ActionDefinition(
        name="connector.status",
        tool="connector",
        action="status",
        intent="Check which apps the user has connected.",
        risk="read",
        requires_approval=False,
    ),
    connection_status,
)

action_gateway.register_action(
    ActionDefinition(
        name=READ_ACTION,
        tool="connector",
        action="composio_read",
        intent="Read data from a connected app through Composio.",
        risk="read",
        requires_approval=False,
    ),
    execute_composio_tool,
)

action_gateway.register_action(
    ActionDefinition(
        name=WRITE_ACTION,
        tool="connector",
        action="composio_action",
        intent="Act in a connected app through Composio.",
        risk="external",
        requires_approval=True,
    ),
    execute_composio_tool,
)
