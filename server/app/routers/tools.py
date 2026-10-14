"""What tools the model could be offered, for the per-bot tool settings UI."""

from typing import Any, Dict, List

from fastapi import APIRouter

from app.config import settings
from app.services.composio_service import composio_service
from app.services.connector_tools import connector_tool_specs
from app.services.llm_tools import ToolSpec, available_tools
from app.services.storage_service import storage_service

router = APIRouter(prefix="/api/v1/tools", tags=["tools"])

GROUP_LABELS = {
    "workspace": "Shared folder",
    "computer": "Computer",
    "share": "File sharing",
    "memory": "Notes",
    "todo": "To-do list",
    "schedule": "Schedules",
    "github": "GitHub (built-in)",
}


def _tool_entry(spec: ToolSpec) -> Dict[str, Any]:
    info = spec.connector or {}
    return {
        "name": spec.name,
        "title": info.get("title") or spec.name,
        "description": spec.description,
        "needs_approval": spec.needs_approval,
    }


@router.get("/catalog")
async def tool_catalog():
    """Built-in tool groups and every connected app's tools.

    Settings on a bot refer to these by group id, toolkit slug, or tool name;
    anything not mentioned is enabled.
    """
    connector_specs = await connector_tool_specs(composio_service)
    builtin = available_tools(
        computer=settings.COMPUTER_PROVIDER == "docker",
        github=bool(composio_service.get_api_key()) and not connector_specs,
    )

    groups: List[Dict[str, Any]] = []
    for spec in builtin:
        group = next((g for g in groups if g["id"] == spec.group), None)
        if group is None:
            group = {"id": spec.group, "label": GROUP_LABELS.get(spec.group, spec.group.title()), "tools": []}
            groups.append(group)
        group["tools"].append(_tool_entry(spec))

    toolkits: List[Dict[str, Any]] = []
    for spec in connector_specs:
        info = spec.connector or {}
        toolkit = next((t for t in toolkits if t["slug"] == info.get("toolkit")), None)
        if toolkit is None:
            toolkit = {"slug": info.get("toolkit"), "name": info.get("toolkit_name") or info.get("toolkit"), "tools": []}
            toolkits.append(toolkit)
        toolkit["tools"].append(_tool_entry(spec))

    disabled = storage_service.get_settings().get("disabled_toolkits") or []
    return {
        "tools_enabled": bool(settings.LLM_TOOLS_ENABLED),
        "groups": groups,
        "toolkits": toolkits,
        "disabled_toolkits": [str(slug) for slug in disabled if isinstance(slug, str)],
    }
