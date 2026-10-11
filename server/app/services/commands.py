"""The slash commands a user can type in chat, and the parser for the ones the
server runs.

Two kinds. `client` commands are handled entirely in the browser (clear the
thread, switch model, stop the reply); they never reach a turn. `action`
commands become a governed action: the server parses them, the action
gateway opens an approval request, and the result is handed to the model as
context for its reply. The registry is one list so the `/` picker in the
composer and the server agree on what exists.
"""

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

from app.services.connector_actions import ConnectorCommandError, parse_connector_command
from app.services.workspace_service import WorkspaceToolError, parse_workspace_command


@dataclass(frozen=True)
class CommandSpec:
    name: str              # typed after the slash
    usage: str             # one line, shown in the picker and in /help
    description: str
    kind: str              # "client" | "action"
    args: str = ""         # hint for the picker; empty means the command takes none
    while_streaming: bool = False  # usable while a reply is still streaming

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


COMMANDS: Tuple[CommandSpec, ...] = (
    CommandSpec("help", "/help", "List the slash commands.", "client"),
    CommandSpec("clear", "/clear", "Delete this bot's conversation history and start fresh.", "client"),
    CommandSpec("model", "/model <model>", "Switch this bot's model. A unique part of the id is enough.", "client", args="<model>"),
    CommandSpec("auto", "/auto off|shadow|on|inherit", "Set this bot's auto-approval mode.", "client", args="off|shadow|on|inherit"),
    CommandSpec("stop", "/stop", "Stop the reply that is streaming.", "client", while_streaming=True),
    CommandSpec("computer", "/computer", "Open this bot's computer.", "client"),
    CommandSpec("audit", "/audit", "Open the audit trail.", "client"),
    CommandSpec("settings", "/settings", "Open App Settings.", "client"),
    CommandSpec(
        "workspace",
        "/workspace list [path] | read <path> | write <path> (content on the next line)",
        "Read or write the shared workspace. Pauses for your approval.",
        "action",
        args="list|read|write",
    ),
    CommandSpec(
        "connector",
        "/connector github issues <owner>/<repo> [open|closed|all] | create-issue <owner>/<repo> <title>",
        "Run a connected GitHub action. Pauses for your approval.",
        "action",
        args="github ...",
    ),
)

_BY_NAME: Dict[str, CommandSpec] = {spec.name: spec for spec in COMMANDS}


class CommandError(ValueError):
    """A slash command the server could not run: unknown, or malformed."""

    def __init__(self, message: str, tool: str = "command"):
        super().__init__(message)
        self.tool = tool


def list_commands() -> List[Dict[str, Any]]:
    return [spec.to_dict() for spec in COMMANDS]


def command_name(text: str) -> Optional[str]:
    """The command a message invokes, or None when it is ordinary text.

    Only a leading slash counts, so a path like `/etc/hosts` mid-sentence or a
    message that merely mentions `/clear` is not a command. A bare `/` is not
    one either.
    """
    stripped = (text or "").lstrip()
    if not stripped.startswith("/"):
        return None
    head = stripped[1:].split(None, 1)[0] if stripped[1:].split(None, 1) else ""
    head = head.split("\n", 1)[0].lower()
    return head or None


def get_command(name: Optional[str]) -> Optional[CommandSpec]:
    return _BY_NAME.get(name) if name else None


def parse_action_command(text: str) -> Optional[Any]:
    """Turn a message into the action it asks for, if it is an action command.

    Returns None for ordinary text (and for a message that only looks like a
    path). Raises CommandError for an unknown command, a client command that
    reached the server anyway, or a malformed action command, with a message
    fit to show the user.
    """
    name = command_name(text)
    if name is None:
        return None
    spec = get_command(name)
    if spec is None:
        raise CommandError(f"Unknown command /{name}. Type / to see the available commands.")
    if spec.kind == "client":
        raise CommandError(f"/{name} is handled by the app, not the model; it was sent as text.")
    try:
        if name == "workspace":
            return parse_workspace_command(text)
        if name == "connector":
            return parse_connector_command(text)
    except WorkspaceToolError as exc:
        raise CommandError(str(exc), tool="workspace") from exc
    except ConnectorCommandError as exc:
        raise CommandError(str(exc), tool="connector") from exc
    raise CommandError(f"/{name} has no server handler.")  # a registry bug, not user error
