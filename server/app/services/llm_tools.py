"""Tools the model may call, each mapped onto a governed gateway action.

The model never executes anything itself. It proposes a call; this module
turns the call into the same `WorkspaceToolCall` / `ActionInvocation` objects
the slash commands produce, and the action gateway applies its deny-by-default
registry and approval rules exactly as before.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

from app.config import settings
from app.services.action_gateway import ActionInvocation
from app.services.computer_provider import FILE_CONTENT_MAX_CHARS, TERMINAL_COMMAND_MAX_CHARS, computer_id_for_bot
from app.services.workspace_service import WorkspaceToolCall


class ToolCallError(ValueError):
    """Raised when the model's tool call cannot be turned into a valid action."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]
    group: str
    needs_approval: bool
    # Set for tools discovered from a connected app (see connector_tools).
    connector: Optional[Dict[str, Any]] = None


_SENSITIVE_ARGUMENT = re.compile(r"(password|secret|token|api[_-]?key|authorization|credential)", re.IGNORECASE)
MAX_CONNECTOR_ARGUMENT_BYTES = 60_000


_SAFE_REPOSITORY_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_INPUT_TYPES = ("click", "keypress", "type", "scroll")


def _obj(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    schema: Dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


# The shared folder is a directory on the host the user chose. These tools
# reach it from the API; a bot whose computer has the folder mounted at
# /shared uses its computer tools there instead and is not offered these.
SHARED_FOLDER_TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="shared_folder_list",
        description="List files and folders in the shared folder, a directory on the host that the user can see. Paths are relative to the folder; omit path for its top level.",
        parameters=_obj({"path": {"type": "string", "description": "Relative directory path, default '.'"}}),
        group="workspace",
        needs_approval=True,
    ),
    ToolSpec(
        name="shared_folder_read",
        description="Read a UTF-8 text file from the shared folder (at most 128 KB).",
        parameters=_obj({"path": {"type": "string", "description": "Relative file path"}}, ["path"]),
        group="workspace",
        needs_approval=True,
    ),
    ToolSpec(
        name="shared_folder_write",
        description="Create or overwrite a UTF-8 text file in the shared folder. Missing parent folders are created automatically.",
        parameters=_obj(
            {
                "path": {"type": "string", "description": "Relative file path"},
                "content": {"type": "string", "description": "Full file contents"},
            },
            ["path", "content"],
        ),
        group="workspace",
        needs_approval=True,
    ),
    ToolSpec(
        name="shared_folder_delete",
        description="Delete a file, or a folder with everything in it, from the shared folder.",
        parameters=_obj({"path": {"type": "string", "description": "Relative path of the file or folder"}}, ["path"]),
        group="workspace",
        needs_approval=True,
    ),
]
WORKSPACE_TOOLS = SHARED_FOLDER_TOOLS  # the group keeps its old id in bot settings

# Where the computer file tools may go. /shared exists when the shared folder
# is mounted into the computers (COMPUTER_DOCKER_HOST_SHARED_DIR).
SHARED_IN_COMPUTER = bool(settings.COMPUTER_DOCKER_HOST_SHARED_DIR)


def _file_roots(shared_in_computer: bool) -> str:
    return "/workspace, /shared or the home directory" if shared_in_computer else "/workspace or the home directory"


_FILE_ROOTS = _file_roots(SHARED_IN_COMPUTER)

COMPUTER_TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="computer_start",
        description="Start this bot's sandboxed computer: a Linux container with a Chromium browser and a shell, with internet access. Call it when another computer tool reports that the computer is not running.",
        parameters=_obj({}),
        group="computer",
        needs_approval=False,
    ),
    ToolSpec(
        name="computer_screenshot",
        description="Capture the sandbox browser screen as an image so you can see the current page. Use it after navigating or sending input.",
        parameters=_obj({}),
        group="computer",
        needs_approval=False,
    ),
    ToolSpec(
        name="computer_browser_navigate",
        description="Open an http(s) URL in the sandbox browser.",
        parameters=_obj({"url": {"type": "string", "description": "Absolute http or https URL"}}, ["url"]),
        group="computer",
        needs_approval=True,
    ),
    ToolSpec(
        name="computer_terminal_execute",
        description="Run a shell command inside the sandbox as its unprivileged user (30 second timeout, 1 MB of output). /workspace and the home directory persist between starts; /tmp does not. There is no sudo: for apt packages use computer_install_packages; pip (in a venv), npm, cargo and similar work normally in the home directory. To create or change files use computer_write_file and computer_edit_file rather than heredocs.",
        parameters=_obj({"command": {"type": "string", "description": "Shell command to run"}}, ["command"]),
        group="computer",
        needs_approval=True,
    ),
    ToolSpec(
        name="computer_files_list",
        description="List files inside the sandbox at a path (default /workspace).",
        parameters=_obj({"path": {"type": "string", "description": "Absolute path inside the sandbox"}}),
        group="computer",
        needs_approval=False,
    ),
    ToolSpec(
        name="computer_read_file",
        description=f"Read a UTF-8 text file inside the sandbox (under {_FILE_ROOTS}, at most 512 KB).",
        parameters=_obj({"path": {"type": "string", "description": "Absolute path inside the sandbox"}}, ["path"]),
        group="computer",
        needs_approval=False,
    ),
    ToolSpec(
        name="computer_write_file",
        description=f"Create or replace a UTF-8 text file inside the sandbox (under {_FILE_ROOTS}). Missing parent directories are created. Give the complete file contents; for a change to an existing file use computer_edit_file.",
        parameters=_obj(
            {
                "path": {"type": "string", "description": "Absolute path inside the sandbox"},
                "content": {"type": "string", "description": "Full file contents"},
            },
            ["path", "content"],
        ),
        group="computer",
        needs_approval=True,
    ),
    ToolSpec(
        name="computer_edit_file",
        description="Change one passage of a text file inside the sandbox: old_text is replaced by new_text. old_text must match the file exactly and only once, so copy it verbatim (computer_read_file shows the file) and include enough surrounding lines to be unique; an empty new_text removes the passage.",
        parameters=_obj(
            {
                "path": {"type": "string", "description": "Absolute path inside the sandbox"},
                "old_text": {"type": "string", "description": "The exact passage to replace"},
                "new_text": {"type": "string", "description": "What it becomes; empty to remove the passage"},
            },
            ["path", "old_text", "new_text"],
        ),
        group="computer",
        needs_approval=True,
    ),
    ToolSpec(
        name="computer_install_packages",
        description="Install Ubuntu packages (apt) into the sandbox. This is the only way to get system packages: there is no sudo in the shell. Installed packages are added to this computer's package list and put back automatically whenever the computer is recreated or its image is rebuilt.",
        parameters=_obj(
            {"packages": {"type": "array", "items": {"type": "string"}, "description": "Ubuntu package names, e.g. [\"ffmpeg\", \"libpq-dev\"]"}},
            ["packages"],
        ),
        group="computer",
        needs_approval=True,
    ),
    ToolSpec(
        name="computer_request_takeover",
        description="Ask the user to take control of your computer for a step that needs a human: a login, a CAPTCHA, a payment, a judgement call, or checking your work. After calling it, tell the user exactly what to do and end your turn; they will message you when they hand control back. While the user has control, your input and navigation tools are refused.",
        parameters=_obj({"reason": {"type": "string", "description": "What you need the user to do, in one or two sentences"}}, ["reason"]),
        group="computer",
        needs_approval=False,
    ),
    ToolSpec(
        name="computer_send_input",
        description="Send pointer or keyboard input to the sandbox browser. Coordinates are page pixels at the screenshot's size (1280x720).",
        parameters=_obj(
            {
                "type": {"type": "string", "enum": list(_INPUT_TYPES)},
                "x": {"type": "number", "description": "click: x coordinate"},
                "y": {"type": "number", "description": "click: y coordinate"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "key": {"type": "string", "description": "keypress: key name such as Enter, Tab, ArrowDown"},
                "text": {"type": "string", "description": "type: text to type, at most 2000 characters"},
                "deltaX": {"type": "number", "description": "scroll: horizontal amount"},
                "deltaY": {"type": "number", "description": "scroll: vertical amount"},
            },
            ["type"],
        ),
        group="computer",
        needs_approval=True,
    ),
]

SHARE_TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="share_file",
        description=(
            "Give the user a file as a download card in the chat. Use it whenever you create or find a file the user "
            "should have (reports, exports, images, archives). `source` is 'workspace' for the shared folder or "
            "'computer' for your computer; `path` is relative to that root (computer paths may start with /workspace/ or /shared/)."
        ),
        parameters=_obj(
            {
                "source": {"type": "string", "enum": ["workspace", "computer"]},
                "path": {"type": "string", "description": "File path inside the chosen root"},
            },
            ["source", "path"],
        ),
        group="share",
        needs_approval=False,
    ),
]

GITHUB_TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="github_list_issues",
        description="List up to 10 issues in a GitHub repository through the connected GitHub account.",
        parameters=_obj(
            {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "state": {"type": "string", "enum": ["open", "closed", "all"], "description": "Default open"},
            },
            ["owner", "repo"],
        ),
        group="github",
        needs_approval=False,
    ),
    ToolSpec(
        name="github_create_issue",
        description="Create an issue in a GitHub repository through the connected GitHub account.",
        parameters=_obj(
            {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "title": {"type": "string", "description": "At most 256 characters"},
                "body": {"type": "string", "description": "Markdown body, at most 10000 characters"},
            },
            ["owner", "repo", "title"],
        ),
        group="github",
        needs_approval=True,
    ),
]


MEMORY_TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="memory_read",
        description="Show your notes: the document you keep for this bot across conversations and compactions.",
        parameters=_obj({}),
        group="memory",
        needs_approval=False,
    ),
    ToolSpec(
        name="memory_write",
        description=(
            f"Replace your notes with new text (at most {settings.BOT_MEMORY_MAX_CHARS} characters; the result echoes the stored notes). "
            "Keep what you will need again later and would otherwise have to rediscover: the project goal, decisions and the reasons, "
            "where things are, how to run and test things, the user's preferences. Organise them as a document with headings so that "
            "later changes can be made with memory_edit; use memory_write only to start the notes or to reorganise them. Drop what is no longer true."
        ),
        parameters=_obj({"text": {"type": "string", "description": "The complete new notes"}}, ["text"]),
        group="memory",
        needs_approval=False,
    ),
    ToolSpec(
        name="memory_edit",
        description=(
            "Change one passage of your notes: old_text is replaced by new_text. old_text must match the current notes exactly and "
            "only once, so copy it verbatim and include enough surrounding text to be unique; an empty new_text removes the passage. "
            "The result shows the changed passage and the new size, not the whole notes. Prefer this over memory_write for any change "
            "smaller than a reorganisation."
        ),
        parameters=_obj(
            {
                "old_text": {"type": "string", "description": "The exact passage to replace, copied from the current notes"},
                "new_text": {"type": "string", "description": "What it becomes; empty to remove the passage"},
            },
            ["old_text", "new_text"],
        ),
        group="memory",
        needs_approval=False,
    ),
]


def available_tools(
    *, computer: bool, github: bool = False, connectors: Optional[List[ToolSpec]] = None
) -> List[ToolSpec]:
    """The built-in tools plus any discovered for connected apps.

    `github` keeps the two hand-written GitHub tools, used only when no live
    connector catalog is available (they are a subset of what Composio offers).
    """
    specs = list(SHARED_FOLDER_TOOLS)
    if computer:
        specs.extend(COMPUTER_TOOLS)
    specs.extend(SHARE_TOOLS)
    specs.extend(MEMORY_TOOLS)
    if connectors:
        taken = {spec.name for spec in specs}
        specs.extend(spec for spec in connectors if spec.name not in taken)
    elif github:
        specs.extend(GITHUB_TOOLS)
    return specs


def filter_tools(
    specs: List[ToolSpec],
    tool_settings: Optional[Dict[str, Any]] = None,
    disabled_toolkits: Optional[List[str]] = None,
) -> List[ToolSpec]:
    """Apply a bot's tool settings and the global app switch-offs.

    `tool_settings` is {"groups": {id: bool}, "toolkits": {slug: bool},
    "tools": {name: bool}}. Only an explicit False disables anything, so a
    bot with no settings gets everything.
    """
    config = tool_settings if isinstance(tool_settings, dict) else {}
    groups = config.get("groups") if isinstance(config.get("groups"), dict) else {}
    toolkits = config.get("toolkits") if isinstance(config.get("toolkits"), dict) else {}
    tools = config.get("tools") if isinstance(config.get("tools"), dict) else {}
    withheld = {str(slug) for slug in (disabled_toolkits or [])}

    kept: List[ToolSpec] = []
    for spec in specs:
        if spec.connector:
            toolkit = spec.connector.get("toolkit")
            if toolkit in withheld or toolkits.get(toolkit) is False:
                continue
        elif groups.get(spec.group) is False:
            continue
        if tools.get(spec.name) is False:
            continue
        kept.append(spec)
    return kept


def without_duplicate_routes(specs: List[ToolSpec], *, shared_in_computer: bool = SHARED_IN_COMPUTER) -> List[ToolSpec]:
    """Drop the host-side shared folder tools when the computer reaches the folder itself.

    Two routes to the same directory, one of them named like the computer's
    own /workspace, is what kept confusing models. With /shared mounted the
    computer tools are the only route a bot with a computer needs.
    """
    if shared_in_computer and any(spec.group == "computer" for spec in specs):
        return [spec for spec in specs if spec.group != "workspace"]
    return specs


def openai_tool_definitions(specs: List[ToolSpec]) -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": spec.name, "description": spec.description, "parameters": spec.parameters},
        }
        for spec in specs
    ]


def describe_tools(
    specs: List[ToolSpec], secret_names: Optional[List[str]] = None, *, shared_in_computer: bool = SHARED_IN_COMPUTER
) -> str:
    """System-prompt paragraph so the model knows what it has and the rules.

    `secret_names` are the user's secrets set as environment variables in
    the bot's computer; the model learns the names, never the values.
    `shared_in_computer` says whether the user's shared folder is mounted
    at /shared inside the computer.
    """
    if not specs:
        return ""
    groups = {spec.group for spec in specs}
    lines = [
        "## Tools",
        "You can call the tools listed in this request. Results come back to you as tool messages; never guess or invent a result you did not receive.",
        "Some calls need the user's approval and pause until the user allows or denies them in the chat. If a call is denied, stop and ask the user how to proceed instead of retrying.",
    ]
    if "workspace" in groups:
        lines.append(
            "- Shared folder: a directory on the host that the user chose and can see. "
            "Use shared_folder_list, shared_folder_read, shared_folder_write and shared_folder_delete for files there."
        )
    if "computer" in groups:
        lines.append(
            "- Your computer: a sandboxed Linux container with a Chromium browser and a shell, with internet access. "
            "Navigate with computer_browser_navigate, then computer_screenshot to see the page, computer_send_input to click or type, "
            "and computer_terminal_execute for commands. The computer starts itself the first time you use it (that first call can take a little longer); computer_start only warms it up. "
            f"Files: computer_read_file, computer_write_file and computer_edit_file handle files under {_file_roots(shared_in_computer)}; "
            "use them instead of heredocs or sed in the terminal. "
            + (
                "/workspace is your own project space. /shared is the user's shared folder: a directory on the host that the user "
                "and other bots see too; put anything the user should find outside the chat there, and nothing else. "
                if shared_in_computer
                else ""
            )
            +
            "Software: computer_install_packages installs apt packages (the shell has no sudo); pip in a venv, npm, cargo and the like work from the shell. "
            "/workspace and the home directory persist between starts, so installed tools and venvs stay; /tmp is wiped. "
            "The user can watch the same desktop live and take control of it; when a step needs a human, call computer_request_takeover."
        )
        if secret_names:
            listed = ", ".join(secret_names)
            lines.append(
                f"- Secrets: the environment variables {listed} are set inside your computer with credentials the user stored. "
                "Use them by name ($NAME in the shell, os.environ in Python); never print, echo or write their values anywhere, "
                "and do not ask the user for them. Their values are redacted from everything you see."
            )
    if "share" in groups:
        lines.append(
            "- Sharing files: share_file puts a download card for a file in the chat. Do it for anything you produce that the user "
            "should keep, then mention the file by name; do not paste large file contents into the reply instead."
        )
    if "memory" in groups:
        lines.append(
            "- Your notes: a document that persists across conversations and compactions. memory_read shows it, memory_edit changes one "
            "passage, memory_write replaces the whole document. Your own writes and edits stay visible in the conversation, the user's edits "
            "appear as notes, and after a compaction the current notes come with the summary, so you do not need to read them at the start "
            "of every turn. Update them when something durable changes (the goal, a decision, where things are, how to run things), not for "
            "transient state, and change the passage concerned with memory_edit rather than rewriting everything."
        )
    if "github" in groups:
        lines.append("- GitHub: github_list_issues and github_create_issue act through the user's connected GitHub account.")
    if "connector" in groups:
        toolkits: Dict[str, str] = {}
        for spec in specs:
            if spec.connector:
                toolkits.setdefault(spec.connector["toolkit"], spec.connector.get("toolkit_name") or spec.connector["toolkit"])
        apps = ", ".join(toolkits[key] for key in sorted(toolkits))
        lines.append(
            f"- Connected apps ({apps}): tools named <app>_<action>, such as {next(s.name for s in specs if s.connector)}, "
            "act through the user's own accounts. Read tools run directly; anything that sends, changes, or deletes asks the user first. "
            "Results are the app's raw API response."
        )
    approval = sorted(spec.name for spec in specs if spec.needs_approval)
    if approval:
        lines.append("Tools that need approval: " + ", ".join(approval) + ".")
    return "\n".join(lines)


# ---- turning a model call into a gateway invocation ---------------------------


def parse_arguments(raw: Any) -> Dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ToolCallError(f"Tool arguments were not valid JSON: {exc.msg}") from exc
        if not isinstance(parsed, dict):
            raise ToolCallError("Tool arguments must be a JSON object.")
        return parsed
    raise ToolCallError("Tool arguments must be a JSON object.")


def _string(args: Dict[str, Any], key: str, *, required: bool = False, default: str = "", max_len: int = 4000) -> str:
    value = args.get(key, None)
    if value is None or value == "":
        if required:
            raise ToolCallError(f"'{key}' is required.")
        return default
    if not isinstance(value, str):
        raise ToolCallError(f"'{key}' must be a string.")
    if len(value) > max_len:
        raise ToolCallError(f"'{key}' is longer than {max_len} characters.")
    return value


def _number(args: Dict[str, Any], key: str, *, required: bool = False, default: float = 0.0) -> float:
    value = args.get(key, None)
    if value is None:
        if required:
            raise ToolCallError(f"'{key}' is required.")
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolCallError(f"'{key}' must be a number.")
    return float(value)


def _computer_file_path(arguments: Dict[str, Any]) -> str:
    path = _string(arguments, "path", required=True, max_len=512).strip()
    if not path.startswith("/"):
        raise ToolCallError("'path' must be an absolute path inside the sandbox, e.g. /workspace/app/main.py.")
    return path


def _computer_invocation(action: str, bot_id: str, arguments: Dict[str, Any], preview: str) -> ActionInvocation:
    computer_id = computer_id_for_bot(bot_id)
    payload = dict(arguments)
    payload.update({"bot_id": bot_id, "computer_id": computer_id})
    return ActionInvocation(
        name=f"computer.{action}",
        arguments=payload,
        target={"bot_id": bot_id, "computer_id": computer_id},
        preview=preview,
    )


def build_invocation(
    name: str,
    arguments: Dict[str, Any],
    bot_id: str,
    specs: Optional[List[ToolSpec]] = None,
) -> Union[WorkspaceToolCall, ActionInvocation]:
    """Validate a model tool call and build the gateway input for it."""
    if name == "shared_folder_list":
        return WorkspaceToolCall("workspace.list", _string(arguments, "path", default=".", max_len=1000))
    if name == "shared_folder_read":
        return WorkspaceToolCall("workspace.read", _string(arguments, "path", required=True, max_len=1000))
    if name == "shared_folder_write":
        return WorkspaceToolCall(
            "workspace.write",
            _string(arguments, "path", required=True, max_len=1000),
            _string(arguments, "content", required=True, max_len=200_000),
        )
    if name == "shared_folder_delete":
        return WorkspaceToolCall("workspace.delete", _string(arguments, "path", required=True, max_len=1000))

    if name == "computer_start":
        return _computer_invocation("start", bot_id, {}, f"Start the computer for {bot_id}")
    if name == "computer_screenshot":
        return _computer_invocation("screenshot", bot_id, {}, f"Capture the computer screen for {bot_id}")
    if name == "computer_request_takeover":
        reason = _string(arguments, "reason", required=True, max_len=500).strip()
        if not reason:
            raise ToolCallError("'reason' is required.")
        return _computer_invocation("request_takeover", bot_id, {"reason": reason}, f"Ask the user to take over the computer for {bot_id}: {reason[:120]}")
    if name == "computer_browser_navigate":
        url = _string(arguments, "url", required=True, max_len=4000).strip()
        if not re.match(r"^https?://[^\s]+$", url, re.IGNORECASE):
            raise ToolCallError("'url' must be an absolute http or https URL.")
        return _computer_invocation("browser_navigate", bot_id, {"url": url}, f"Navigate the computer for {bot_id} to {url}")
    if name == "computer_terminal_execute":
        command = _string(arguments, "command", required=True, max_len=TERMINAL_COMMAND_MAX_CHARS)
        return _computer_invocation("terminal_execute", bot_id, {"command": command}, f"Run in the computer for {bot_id}: {command[:120]}")
    if name == "computer_read_file":
        path = _computer_file_path(arguments)
        return _computer_invocation("files_read", bot_id, {"path": path}, f"Read {path} from the computer for {bot_id}")
    if name == "computer_write_file":
        path = _computer_file_path(arguments)
        content = _string(arguments, "content", required=True, max_len=FILE_CONTENT_MAX_CHARS)
        return _computer_invocation(
            "files_write", bot_id, {"path": path, "content": content}, f"Write {len(content)} characters to {path} in the computer for {bot_id}"
        )
    if name == "computer_edit_file":
        path = _computer_file_path(arguments)
        old_text = _string(arguments, "old_text", required=True, max_len=FILE_CONTENT_MAX_CHARS)
        new_text = _string(arguments, "new_text", default="", max_len=FILE_CONTENT_MAX_CHARS)
        return _computer_invocation(
            "files_edit",
            bot_id,
            {"path": path, "old_text": old_text, "new_text": new_text},
            f"Edit {path} in the computer for {bot_id} ({len(old_text)} characters become {len(new_text)})",
        )
    if name == "computer_files_list":
        path = _string(arguments, "path", default="/workspace", max_len=1000)
        return _computer_invocation("files_list", bot_id, {"path": path}, f"List computer files for {bot_id} at {path}")
    if name == "computer_install_packages":
        packages = arguments.get("packages")
        if isinstance(packages, str):
            packages = packages.split()
        if not isinstance(packages, list) or not packages or len(packages) > 50:
            raise ToolCallError("'packages' must be a list of 1 to 50 package names.")
        names = []
        for item in packages:
            name = item.strip().lower() if isinstance(item, str) else ""
            if not re.match(r"^[a-z0-9][a-z0-9+.:~-]{0,99}$", name):
                raise ToolCallError(f"'{str(item)[:60]}' is not a valid package name.")
            if name not in names:
                names.append(name)
        listed = ", ".join(names)
        return _computer_invocation("install_packages", bot_id, {"packages": names}, f"Install packages in the computer for {bot_id}: {listed[:160]}")
    if name == "computer_send_input":
        kind = _string(arguments, "type", required=True, max_len=20).lower()
        if kind not in _INPUT_TYPES:
            raise ToolCallError("'type' must be one of click, keypress, type, scroll.")
        event: Dict[str, Any] = {"type": kind}
        if kind == "click":
            event["x"] = _number(arguments, "x", required=True)
            event["y"] = _number(arguments, "y", required=True)
            button = _string(arguments, "button", default="left", max_len=10)
            if button not in {"left", "right", "middle"}:
                raise ToolCallError("'button' must be left, right, or middle.")
            event["button"] = button
            preview = f"Click at ({int(event['x'])}, {int(event['y'])}) in the computer for {bot_id}"
        elif kind == "keypress":
            event["key"] = _string(arguments, "key", required=True, max_len=40)
            preview = f"Press {event['key']} in the computer for {bot_id}"
        elif kind == "type":
            event["text"] = _string(arguments, "text", required=True, max_len=2000)
            preview = f"Type {len(event['text'])} characters in the computer for {bot_id}"
        else:
            event["deltaX"] = _number(arguments, "deltaX")
            event["deltaY"] = _number(arguments, "deltaY")
            preview = f"Scroll by ({int(event['deltaX'])}, {int(event['deltaY'])}) in the computer for {bot_id}"
        return _computer_invocation("send_input", bot_id, {"event": event}, preview)

    if name == "share_file":
        source = _string(arguments, "source", required=True, max_len=20).strip().lower()
        if source not in {"workspace", "computer"}:
            raise ToolCallError("'source' must be workspace or computer.")
        path = _string(arguments, "path", required=True, max_len=1000).strip()
        if not path:
            raise ToolCallError("'path' is required.")
        where = "the shared workspace" if source == "workspace" else f"the computer for {bot_id}"
        return ActionInvocation(
            name="files.share",
            arguments={"source": source, "path": path},
            target={"bot_id": bot_id, "source": source},
            preview=f"Share {path} from {where} with the user",
        )

    if name == "memory_read":
        return ActionInvocation(
            name="memory.read", arguments={"bot_id": bot_id}, target={"bot_id": bot_id}, preview=f"Read the notes of {bot_id}"
        )
    if name == "memory_write":
        text = _string(arguments, "text", required=True, max_len=settings.BOT_MEMORY_MAX_CHARS)
        return ActionInvocation(
            name="memory.write",
            arguments={"bot_id": bot_id, "text": text},
            target={"bot_id": bot_id},
            preview=f"Update the notes of {bot_id} ({len(text)} characters)",
        )
    if name == "memory_edit":
        old_text = _string(arguments, "old_text", required=True, max_len=settings.BOT_MEMORY_MAX_CHARS)
        new_text = _string(arguments, "new_text", default="", max_len=settings.BOT_MEMORY_MAX_CHARS)
        if not old_text.strip():
            raise ToolCallError("'old_text' is the passage to replace and cannot be empty.")
        return ActionInvocation(
            name="memory.edit",
            arguments={"bot_id": bot_id, "old_text": old_text, "new_text": new_text},
            target={"bot_id": bot_id},
            preview=f"Edit the notes of {bot_id} ({len(old_text)} characters become {len(new_text)})",
        )

    if name in {"github_list_issues", "github_create_issue"}:
        owner = _string(arguments, "owner", required=True, max_len=100)
        repo = _string(arguments, "repo", required=True, max_len=100)
        if not _SAFE_REPOSITORY_PART.fullmatch(owner) or not _SAFE_REPOSITORY_PART.fullmatch(repo):
            raise ToolCallError("Repository owner and name may contain only letters, numbers, dots, dashes, and underscores.")
        if name == "github_list_issues":
            state = _string(arguments, "state", default="open", max_len=10).lower()
            if state not in {"open", "closed", "all"}:
                raise ToolCallError("'state' must be open, closed, or all.")
            return ActionInvocation(
                name="connector.github_list_issues",
                arguments={"owner": owner, "repo": repo, "state": state, "per_page": 10},
                target={"connector": "github", "repository": f"{owner}/{repo}"},
                preview=f"List {state} GitHub issues in {owner}/{repo}",
            )
        title = _string(arguments, "title", required=True, max_len=256).strip()
        if not title:
            raise ToolCallError("'title' is required.")
        body = _string(arguments, "body", default="", max_len=10000)
        return ActionInvocation(
            name="connector.github_create_issue",
            arguments={"owner": owner, "repo": repo, "title": title, "body": body},
            target={"connector": "github", "repository": f"{owner}/{repo}"},
            preview=f"Create a GitHub issue in {owner}/{repo}: {title}",
            display_arguments={"owner": owner, "repo": repo, "title": title, "body_bytes": len(body.encode("utf-8"))},
        )

    if specs:
        spec = next((s for s in specs if s.name == name and s.connector), None)
        if spec is not None:
            return connector_invocation(spec, arguments)

    raise ToolCallError(f"Unknown tool: {name}")


def connector_invocation(spec: ToolSpec, arguments: Dict[str, Any]) -> ActionInvocation:
    """Gateway input for a tool discovered from a connected app."""
    info = spec.connector or {}
    if not isinstance(arguments, dict):
        raise ToolCallError("Tool arguments must be a JSON object.")
    encoded = json.dumps(arguments, ensure_ascii=False, default=str)
    if len(encoded.encode("utf-8")) > MAX_CONNECTOR_ARGUMENT_BYTES:
        raise ToolCallError(f"Tool arguments exceed {MAX_CONNECTOR_ARGUMENT_BYTES} bytes.")
    allowed = set((spec.parameters.get("properties") or {}).keys())
    unknown = sorted(key for key in arguments if key not in allowed)
    if unknown and allowed:
        raise ToolCallError(f"Unknown argument(s) for {spec.name}: {', '.join(unknown)}")
    missing = [key for key in spec.parameters.get("required") or [] if key not in arguments]
    if missing:
        raise ToolCallError(f"Missing required argument(s) for {spec.name}: {', '.join(missing)}")

    # A short, safe preview: a few scalar arguments, never anything secret-like.
    shown = []
    for key, value in arguments.items():
        if len(shown) >= 3 or _SENSITIVE_ARGUMENT.search(key) or isinstance(value, (dict, list)):
            continue
        text = str(value)
        shown.append(f"{key}={text[:40]}{'...' if len(text) > 40 else ''}")
    title = info.get("title") or spec.name
    preview = f"{info.get('toolkit_name') or info.get('toolkit', 'app')}: {title}"
    if shown:
        preview = f"{preview} ({', '.join(shown)})"
    action_name = "connector.composio_read" if info.get("read_only") else "connector.composio_action"
    gateway_arguments: Dict[str, Any] = {"tool": info["slug"], "arguments": arguments}
    if info.get("version"):
        gateway_arguments["version"] = info["version"]
    return ActionInvocation(
        name=action_name,
        arguments=gateway_arguments,
        target={"connector": info.get("toolkit"), "tool": info["slug"]},
        preview=preview[:300],
        display_arguments=arguments,
    )
