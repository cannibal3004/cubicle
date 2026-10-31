"""Tools the model may call, each mapped onto a governed gateway action.

The model never executes anything itself. It proposes a call; this module
turns the call into the same `WorkspaceToolCall` / `ActionInvocation` objects
the slash commands produce, and the action gateway applies its deny-by-default
registry and approval rules exactly as before.
"""

import json
import re
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Union

from app.config import settings
from app.services.tool_names import RENAMED, current_name, current_names, unknown_message
from app.services.action_gateway import ActionInvocation
from app.services.computer_provider import (
    FILE_CONTENT_MAX_CHARS,
    TERMINAL_COMMAND_MAX_CHARS,
    ComputerProviderError,
    check_package_names,
    computer_id_for_bot,
    check_file_path,
    check_timeout,
)
from app.services.file_share import normalize_source
from app.services.workspace_service import WorkspaceToolCall
from app.services.bot_todo import STATES as TODO_STATES
from app.services.schedule_service import ScheduleError, clean_cadence, clean_check, describe_cadence


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
    # The user trusts this tool for the bot: it runs without an approval card.
    trusted: bool = False


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


_PATH_HINT = "Path inside the sandbox; a relative path is taken from /workspace, ~ is the home directory"


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
        description=f"Open an http(s) URL in the sandbox browser (gives up after {int(settings.COMPUTER_DOCKER_COMMAND_TIMEOUT)} seconds if the page does not load).",
        parameters=_obj({"url": {"type": "string", "description": "Absolute http or https URL"}}, ["url"]),
        group="computer",
        needs_approval=True,
    ),
    ToolSpec(
        name="computer_terminal_execute",
        description=f"Run a shell command inside the sandbox as its unprivileged user. Long output comes back as its start and end, with the whole of it saved to a file you can grep or read in parts (up to 50 MB). It may run for {int(settings.COMPUTER_DOCKER_COMMAND_TIMEOUT)} seconds unless you set timeout_seconds: longer (up to {int(settings.COMPUTER_DOCKER_COMMAND_TIMEOUT_MAX)}) for builds, installs and test runs, shorter for probes of hosts that may hang. A command that hits its limit is stopped, with everything it started, and returns exit code 124 with its output so far. For anything long or that keeps running (a big copy or download, a build, a scan, a server, a watcher), set background: true: the call returns at once with a job id, a log file and the commands to check on it or stop it, and the job runs on. When a background job ends you get a message in this conversation with its exit code and the end of its log (it starts a turn if you are idle, or comes after the reply in progress), so you need not poll it or wait for it: carry on, or end your reply. Set notify: false only for a job that never ends on its own, like a server. Do not end a normal command with `server &`: send a background process's output to a file or use background. /workspace and the home directory persist between starts; /tmp does not. There is no sudo: for apt packages use computer_install_packages; pip (in a venv), npm, cargo and similar work normally in the home directory. To create or change files use write_file and edit_file rather than heredocs. Your secrets are set as environment variables in every command.",
        parameters=_obj(
            {
                "command": {"type": "string", "description": "Shell command to run"},
                "timeout_seconds": {"type": "integer", "description": f"How long it may run, in seconds (default {int(settings.COMPUTER_DOCKER_COMMAND_TIMEOUT)}, at most {int(settings.COMPUTER_DOCKER_COMMAND_TIMEOUT_MAX)})"},
                "background": {"type": "boolean", "description": "Start it as a background job and return at once with its log file (no time limit)"},
                "notify": {"type": "boolean", "description": "With background: be told in this conversation when it ends (default true; false only for a job that runs on for good, like a server)"},
            },
            ["command"],
        ),
        group="computer",
        needs_approval=True,
    ),
    ToolSpec(
        name="list_files",
        description="List the files and folders in your computer at a path (default /workspace).",
        parameters=_obj({"path": {"type": "string", "description": _PATH_HINT}}),
        group="computer",
        needs_approval=False,
    ),
    ToolSpec(
        name="read_file",
        description=f"Read a UTF-8 text file in your computer (under {_FILE_ROOTS}, at most 512 KB).",
        parameters=_obj({"path": {"type": "string", "description": _PATH_HINT}}, ["path"]),
        group="computer",
        needs_approval=False,
    ),
    ToolSpec(
        name="write_file",
        description=f"Create or replace a UTF-8 text file in your computer (under {_FILE_ROOTS}). Missing parent directories are created. Give the complete file contents; for a change to an existing file use edit_file. A shell or Python script is checked as it is saved (ShellCheck, or Ruff for errors): fix what the result's check reports before running it.",
        parameters=_obj(
            {
                "path": {"type": "string", "description": _PATH_HINT},
                "content": {"type": "string", "description": "Full file contents"},
            },
            ["path", "content"],
        ),
        group="computer",
        needs_approval=True,
    ),
    ToolSpec(
        name="edit_file",
        description="Change one passage of a text file in your computer: old_text is replaced by new_text. old_text must match the file exactly and only once, so copy it verbatim (read_file shows the file) and include enough surrounding lines to be unique; an empty new_text removes the passage.",
        parameters=_obj(
            {
                "path": {"type": "string", "description": _PATH_HINT},
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
        description="Send pointer or keyboard input to the sandbox desktop. Coordinates are pixels of the latest screenshot (its width and height are in the screenshot result).",
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
            "should have (reports, exports, images, archives). `source` is 'computer' for a file in your computer (its "
            "/workspace or home) or 'shared' for the shared folder; `path` is relative to that root, and a path starting with "
            "/workspace/ or /shared/ says where the file is by itself. A file missing from the chosen place is looked for in the other."
        ),
        parameters=_obj(
            {
                "source": {"type": "string", "enum": ["computer", "shared"]},
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
        description="Show your notes: the document of durable facts about your job, shared by all your conversations.",
        parameters=_obj({}),
        group="memory",
        needs_approval=False,
    ),
    ToolSpec(
        name="memory_write",
        description=(
            f"Replace your notes with new text (at most {settings.BOT_MEMORY_MAX_CHARS} characters; the result echoes the stored notes). "
            "Your notes are durable facts about your job that every conversation needs and you would otherwise have to rediscover: the "
            "systems you look after and how to reach them, house rules, how things are done here, lessons learned. Not the state of any "
            "piece of work (work notes hold that), not a history of what was done (the project log), and nothing about one person or one "
            "task. Organise them as a document with headings so that later changes can be made with memory_edit; use memory_write only to "
            "start the notes or to reorganise them. Drop what is no longer true."
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


_PROJECT_ARG = {"type": "string", "description": "A project by name, when it is not the one this conversation is in (optional)"}

PROJECT_TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="work_notes_read",
        description="Show your work notes: where the current piece of work stands. They are this conversation's project's, or this conversation's own when it is in no project.",
        parameters=_obj({"project": _PROJECT_ARG}),
        group="projects",
        needs_approval=False,
    ),
    ToolSpec(
        name="work_notes_write",
        description=(
            f"Replace your work notes (at most {settings.BOT_WORK_NOTES_MAX_CHARS} characters): where the work stands now, what is decided, "
            "what is next and what is blocked, as someone picking the work up would need it first. Keep them current, not cumulative: what "
            "happened along the way goes to project_log, reference detail (inventories, configs, long write-ups) to files in the project's "
            "folder that the work notes point to, durable facts about your job to your notes (memory_edit). Use work_notes_edit for "
            "smaller changes."
        ),
        parameters=_obj({"text": {"type": "string", "description": "The complete new work notes"}, "project": _PROJECT_ARG}, ["text"]),
        group="projects",
        needs_approval=False,
    ),
    ToolSpec(
        name="work_notes_edit",
        description=(
            "Change one passage of your work notes: old_text, copied exactly and matching once, is replaced by new_text (empty removes it)."
        ),
        parameters=_obj(
            {
                "old_text": {"type": "string", "description": "The exact passage to replace, copied from the current work notes"},
                "new_text": {"type": "string", "description": "What it becomes; empty to remove the passage"},
                "project": _PROJECT_ARG,
            },
            ["old_text", "new_text"],
        ),
        group="projects",
        needs_approval=False,
    ),
    ToolSpec(
        name="project_log",
        description=(
            "Add a dated entry to the project's log: what was done, found or decided, in a few lines. The log stays out of your context; "
            "entries come back when a later message matches them, or with project_log_read. Use it instead of piling history into your notes."
        ),
        parameters=_obj({"entry": {"type": "string", "description": "What happened, in a few lines"}, "project": _PROJECT_ARG}, ["entry"]),
        group="projects",
        needs_approval=False,
    ),
    ToolSpec(
        name="project_log_read",
        description="Read the project's log: the newest entries, or the ones that match a query.",
        parameters=_obj(
            {
                "query": {"type": "string", "description": "What to look for (optional; without it, the newest entries)"},
                "limit": {"type": "integer", "description": "How many of the newest entries (default 8)"},
                "project": _PROJECT_ARG,
            }
        ),
        group="projects",
        needs_approval=False,
    ),
    ToolSpec(
        name="projects_list",
        description="List your projects: each one's status line, size of its work notes and log, and which one this conversation is in.",
        parameters=_obj({}),
        group="projects",
        needs_approval=False,
    ),
    ToolSpec(
        name="project_create",
        description="Create a project: a named piece of work with its own work notes and log. This conversation does not move into it; project_join does that.",
        parameters=_obj(
            {"name": {"type": "string", "description": "A short name"}, "work_notes": {"type": "string", "description": "Its starting work notes (optional)"}},
            ["name"],
        ),
        group="projects",
        needs_approval=False,
    ),
    ToolSpec(
        name="project_join",
        description="Put this conversation in a project, so its work notes and log are this conversation's; an empty name takes it out of its project.",
        parameters=_obj({"name": {"type": "string", "description": "The project's name; empty to leave the current one"}}, ["name"]),
        group="projects",
        needs_approval=False,
    ),
]


# Skills (services/skills): the library's (installed or written in App Settings) and the bot's
# own (what it wrote, most often after the user showed it a task; these were procedures).
SKILL_TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="skill_read",
        description=(
            "Load one of your skills by name (listed in your tools prompt): its instructions in full, and the folder in your computer "
            "with its scripts and files. Read it before a task it fits, then follow it. With 'file', read one of its text files "
            "(a reference or template the instructions point to)."
        ),
        parameters=_obj(
            {
                "name": {"type": "string", "description": "The skill's name (a unique part is enough)"},
                "file": {"type": "string", "description": "A file of the skill to read, by its path in the skill (e.g. reference/forms.md)"},
            },
            ["name"],
        ),
        group="skills",
        needs_approval=False,
    ),
    ToolSpec(
        name="skill_save",
        description=(
            "Keep a skill of your own: how to do a kind of task, written from what the user showed you or what you worked out, so you "
            "can do it again. Numbered steps in your own words; for work in the computer's browser, each naming the page or URL and the "
            "exact label of the control, with what to type or choose. Leave out passwords. Saving under an existing name replaces it."
        ),
        parameters=_obj(
            {
                "name": {"type": "string", "description": "Short, says what it achieves (e.g. 'File a PTO request in Workday')"},
                "description": {"type": "string", "description": "When to use it, in a line"},
                "instructions": {"type": "string", "description": "The numbered steps"},
            },
            ["name", "instructions"],
        ),
        group="skills",
        needs_approval=False,
    ),
]


TODO_TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="todo_list",
        description="Show your to-do list: the items you are working through, each with its id and state (open, doing, done).",
        parameters=_obj({}),
        group="todo",
        needs_approval=False,
    ),
    ToolSpec(
        name="todo_add",
        description=(
            f"Add one or more items to your to-do list (at most {settings.BOT_TODO_MAX_ITEMS} items in all; the result shows the whole list "
            "with the new ids). Write the list before starting a job with several steps, one short item per step, and add steps as "
            "you discover them."
        ),
        parameters=_obj(
            {"items": {"type": "array", "items": {"type": "string"}, "description": "The new items, one short line each"}},
            ["items"],
        ),
        group="todo",
        needs_approval=False,
    ),
    ToolSpec(
        name="todo_update",
        description=(
            "Change one item of your to-do list by id: set its state (open, doing, done), reword it, or remove it. Mark an item doing "
            "when you start it and done as soon as it is finished, so the list always says where you are. The result shows the whole list."
        ),
        parameters=_obj(
            {
                "id": {"type": "string", "description": "The item's id, e.g. t3"},
                "state": {"type": "string", "enum": ["open", "doing", "done"], "description": "The new state"},
                "text": {"type": "string", "description": "New wording for the item"},
                "remove": {"type": "boolean", "description": "true to remove the item instead"},
            },
            ["id"],
        ),
        group="todo",
        needs_approval=False,
    ),
]


SCHEDULE_TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="schedule_list",
        description="Show your schedules (prompts set to run later in your Scheduled conversation, and their checks) with how each last ran, the current time and timezone.",
        parameters=_obj({}),
        group="schedule",
        needs_approval=False,
    ),
    ToolSpec(
        name="schedule_create",
        description=(
            "Schedule a prompt to run later in your Scheduled conversation: once at a time (or in a number of minutes), every N minutes, "
            "daily at a time, or weekly on chosen days at a time. The run starts a fresh turn with the prompt as the user's message, so the "
            "prompt must say everything needed to do the job from scratch: what to do, where things are, what to report. To watch for "
            "something, add check_script: a shell script run in your computer at each time without you, which wakes you (the prompt plus "
            "what it printed) only when it prints something or fails, and only when that differs from its last report. Call schedule_list "
            "first when the time matters; it gives the current time and timezone. The user approves each new schedule."
        ),
        parameters=_obj(
            {
                "name": {"type": "string", "description": "A short name, e.g. Morning digest"},
                "prompt": {"type": "string", "description": "The message to run, complete in itself"},
                "kind": {"type": "string", "enum": ["once", "every", "daily", "weekly"]},
                "at": {"type": "string", "description": "once: the date and time, YYYY-MM-DD HH:MM in the app's timezone"},
                "in_minutes": {"type": "integer", "description": "once: alternatively, how many minutes from now"},
                "minutes": {"type": "integer", "description": "every: the interval in minutes (at least 5)"},
                "time": {"type": "string", "description": "daily and weekly: the time of day, HH:MM"},
                "days": {"type": "array", "items": {"type": "integer"}, "description": "weekly: the weekdays, 0 = Monday to 6 = Sunday"},
                "check_script": {
                    "type": "string",
                    "description": "Optional shell script run first; print nothing and exit 0 when all is well, print what is wrong or new otherwise",
                },
                "check_timeout_seconds": {"type": "integer", "description": "How long the check may run, 5 to 300 (default 60)"},
            },
            ["name", "prompt", "kind"],
        ),
        group="schedule",
        needs_approval=True,
    ),
    ToolSpec(
        name="schedule_update",
        description=(
            "Change one of your schedules by id: its name, prompt, timing, check script (an empty check_script removes the check) or "
            "whether it is enabled. Give only what changes; timing fields replace the whole timing. The user approves."
        ),
        parameters=_obj(
            {
                "id": {"type": "string", "description": "The schedule id, e.g. sch-1a2b3c4d"},
                "name": {"type": "string"},
                "prompt": {"type": "string"},
                "kind": {"type": "string", "enum": ["once", "every", "daily", "weekly"]},
                "at": {"type": "string"},
                "in_minutes": {"type": "integer"},
                "minutes": {"type": "integer"},
                "time": {"type": "string"},
                "days": {"type": "array", "items": {"type": "integer"}},
                "check_script": {"type": "string", "description": "The new check script; empty to remove the check"},
                "check_timeout_seconds": {"type": "integer"},
                "enabled": {"type": "boolean"},
            },
            ["id"],
        ),
        group="schedule",
        needs_approval=True,
    ),
    ToolSpec(
        name="schedule_delete",
        description="Delete one of your schedules by id (schedule_list shows them). The user approves.",
        parameters=_obj({"id": {"type": "string", "description": "The schedule id, e.g. sch-1a2b3c4d"}}, ["id"]),
        group="schedule",
        needs_approval=True,
    ),
]


# Cubicle's own help pages (services/help.py), searched for the user's how-do-I questions.
HELP_TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="cubicle_help",
        description=(
            "Look up how Cubicle, the app you and the user are in, works: its help pages, searched for the question. Use it when the "
            "user asks how to do something in Cubicle (share a bot, let a command run without asking, set up a schedule, connect an "
            "app) or what a part of it does. The result has the matching sections, links to the pages, screenshots you can include, "
            "and how you yourself are set up, so you can answer for this bot rather than in general."
        ),
        parameters=_obj({"question": {"type": "string", "description": "The question, in the user's words or close to them"}}, ["question"]),
        group="help",
        needs_approval=False,
    ),
]


# Connecting apps through Composio, with Cubicle's own flow (the same one the
# marketplace uses). Composio's COMPOSIO_MANAGE_CONNECTIONS only works inside
# a Composio tool-router session, which this app does not use.
_TOOLKIT_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

QUESTION_TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="ask_user",
        description=(
            "Ask the user one question and wait for the answer; the reply pauses until they respond. Use it when a decision is "
            "theirs to make or when something you need is missing and cannot be found with your other tools. Offer 2 to 6 short "
            "options when there are natural choices (the user can still type something else unless allow_other is false); leave "
            "options out for a free-text answer. Ask one thing at a time, and do not ask what you could find out yourself."
        ),
        parameters=_obj(
            {
                "question": {"type": "string", "description": "The question, in one or two sentences"},
                "options": {"type": "array", "items": {"type": "string"}, "description": "2 to 6 short choices, in the order to show them"},
                "allow_other": {"type": "boolean", "description": "Whether the user may type an answer of their own (default true)"},
            },
            ["question"],
        ),
        group="questions",
        needs_approval=False,
    ),
]

ORCHESTRATION_TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="bots_list",
        description="The user's other bots: id, name, role, whether each is idle, working or waiting for the user, open to-do items and last activity. Start here before delegating; give `task` to have them ranked by fit.",
        parameters=_obj({"task": {"type": "string", "description": "Optional: the job to delegate, in a sentence; the bots come back best match first with a relevance score"}}),
        group="orchestration",
        needs_approval=False,
    ),
    ToolSpec(
        name="bots_status",
        description="One bot in detail: its summary of earlier work, to-do list, notes, recent messages (with the tools it used), computer state.",
        parameters=_obj({"bot_id": {"type": "string"}}, ["bot_id"]),
        group="orchestration",
        needs_approval=False,
    ),
    ToolSpec(
        name="bots_create",
        description=(
            "Create a new bot for a kind of work none of the existing bots covers (the user approves). Give it a clear role and a focused "
            "system prompt that says what it is for and how to work; it gets the usual tools and its own computer."
        ),
        parameters=_obj(
            {
                "name": {"type": "string", "description": "Short and distinct"},
                "role": {"type": "string", "description": "One line, e.g. Security researcher"},
                "description": {"type": "string"},
                "system_prompt": {"type": "string", "description": "Its standing instructions"},
                "avatar": {"type": "string", "description": "One emoji"},
                "model": {"type": "string", "description": "Leave out for the default model"},
            },
            ["name", "role", "system_prompt"],
        ),
        group="orchestration",
        needs_approval=True,
    ),
    ToolSpec(
        name="bots_send",
        description=(
            "Hand a bot a task (the user approves). The message is posted into that bot's chat as coming from you and its reply starts at "
            "once; write everything it needs to act on its own, as you would brief a colleague. With wait_seconds (default 600, at most 1800) "
            "you get its reply back here. If it takes longer, its reply is posted into this conversation when it finishes and you then "
            "report to the user; bots_reply checks on it sooner. If the bot stops at an approval or a question, you are told and the user "
            "must answer it in that bot's chat."
        ),
        parameters=_obj(
            {
                "bot_id": {"type": "string"},
                "message": {"type": "string", "description": "The task, complete and self-contained"},
                "wait_seconds": {"type": "number", "description": "How long to wait for the reply; 0 to return at once"},
            },
            ["bot_id", "message"],
        ),
        group="orchestration",
        needs_approval=True,
    ),
    ToolSpec(
        name="bots_reply",
        description="A bot's latest reply (or its reply to one of your messages, by message_id) and whether it is idle, working or waiting for the user.",
        parameters=_obj({"bot_id": {"type": "string"}, "message_id": {"type": "string", "description": "From bots_send, to get the reply to that message"}}, ["bot_id"]),
        group="orchestration",
        needs_approval=False,
    ),
]

CONNECTOR_TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="connector_connect",
        description=(
            "Start connecting an app through Composio for the user, by toolkit slug (find slugs with composio_search_tools "
            "or composio_retrieve_toolkits). The result carries a sign-in link: give the user that link as a Markdown link and "
            "stop there; the app's own tools are offered from the turn after they sign in. An app that needs no sign-in is "
            "connected at once. The user approves each new connection."
        ),
        parameters=_obj({"toolkit": {"type": "string", "description": "The toolkit slug, e.g. googledrive"}}, ["toolkit"]),
        group="connectors",
        needs_approval=True,
    ),
    ToolSpec(
        name="connector_status",
        description="Whether the user has active connections for the given toolkits (by slug), e.g. after they say they signed in.",
        parameters=_obj({"toolkits": {"type": "array", "items": {"type": "string"}, "description": "Toolkit slugs to check"}}, ["toolkits"]),
        group="connectors",
        needs_approval=False,
    ),
]


# Managing MCP servers: a bot can set one up when asked. Secret values never
# pass through the model; a value written as $NAME refers to an app secret.
MCP_TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="mcp_list_servers",
        description="List the configured MCP servers with their status and tool counts.",
        parameters=_obj({}),
        group="mcp",
        needs_approval=False,
    ),
    ToolSpec(
        name="mcp_add_server",
        description=(
            "Add an MCP server (the user approves). scope 'computer' (preferred for a command) runs a command inside your own computer, "
            "next to your files and the packages you installed there; only you get its tools, and $NAME in env resolves from the computer's "
            "environment (the user's secrets). scope 'bot' connects to a url (streamable HTTP, or SSE) for you alone, $NAME in headers "
            "being your secrets and the user's. scope 'user' runs a stdio command in the host API or connects to a url, for every bot of "
            "the user. For an API key, never ask the user to paste it into the chat: tell them to add it as a secret and write $NAME as the "
            "env or header value. The server is connected and tested at once; its tools are offered from your next turn. Set "
            "trust_read_only only for servers that cannot change anything (documentation, search). A server with many tools (some "
            "list hundreds): name the ones the job needs in 'tools' (as the server lists them; mcp_test_server shows them all) and "
            "only those are offered."
        ),
        parameters=_obj(
            {
                "name": {"type": "string", "description": "A short name, e.g. Context7"},
                "scope": {"type": "string", "enum": ["computer", "bot", "user"], "description": "computer: a command in your own computer, only for you (default); bot: a url, only for you; user: for every bot of the user"},
                "transport": {"type": "string", "enum": ["stdio", "http", "sse"], "description": "scope user or bot; a computer server is always a command. http: a URL, streamable HTTP or the older SSE protocol, whichever answers; sse: SSE only"},
                "command": {"type": "string", "description": "stdio: the program to run, e.g. npx or uvx"},
                "args": {"type": "array", "items": {"type": "string"}, "description": "stdio: its arguments"},
                "env": {"type": "object", "additionalProperties": {"type": "string"}, "description": "stdio: environment variables; use $NAME for an app secret"},
                "url": {"type": "string", "description": "http: the server URL"},
                "headers": {"type": "object", "additionalProperties": {"type": "string"}, "description": "http: request headers; use $NAME for an app secret"},
                "trust_read_only": {"type": "boolean", "description": "Treat every tool of this server as read-only (no approval)"},
                "tools": {"type": "array", "items": {"type": "string"}, "description": "Offer only these of its tools (names as the server lists them); omit to offer all"},
            },
            ["name"],
        ),
        group="mcp",
        needs_approval=True,
    ),
    ToolSpec(
        name="mcp_test_server",
        description="Connect to a configured MCP server by id and list all its tools, each marked offered or not (a server may offer only some), to check it works or see what it has.",
        parameters=_obj({"id": {"type": "string", "description": "The server id, e.g. mcp-1a2b3c4d"}}, ["id"]),
        group="mcp",
        needs_approval=False,
    ),
    ToolSpec(
        name="mcp_remove_server",
        description="Remove a configured MCP server by id (the user approves).",
        parameters=_obj({"id": {"type": "string"}}, ["id"]),
        group="mcp",
        needs_approval=True,
    ),
]


def available_tools(
    *,
    computer: bool,
    github: bool = False,
    connectors: Optional[List[ToolSpec]] = None,
    connector_management: bool = False,
    orchestrator: bool = False,
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
    specs.extend(SKILL_TOOLS)
    specs.extend(PROJECT_TOOLS)
    specs.extend(TODO_TOOLS)
    specs.extend(SCHEDULE_TOOLS)
    specs.extend(QUESTION_TOOLS)
    specs.extend(HELP_TOOLS)
    if orchestrator:
        specs.extend(ORCHESTRATION_TOOLS)
    if connector_management:
        specs.extend(CONNECTOR_TOOLS)
    specs.extend(MCP_TOOLS)
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
    """Apply a bot's tool settings over the apps' defaults.

    `tool_settings` is {"groups": {id: bool}, "toolkits": {slug: bool},
    "tools": {name: bool}}. Only an explicit False disables anything, so a
    bot with no settings gets everything, except the apps in
    `disabled_toolkits`: those are off by default, and a bot gets one only
    with an explicit True for it.
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
            choice = toolkits.get(toolkit)
            if choice is False or (choice is not True and toolkit in withheld):
                continue
        elif groups.get(spec.group) is False:
            continue
        if tools.get(spec.name) is False or any(tools.get(old) is False for old, new in RENAMED.items() if new == spec.name):
            continue
        kept.append(spec)
    return kept


def trust_tools(specs: List[ToolSpec], trusted: Optional[List[str]]) -> List[ToolSpec]:
    """Mark the bot's trusted tools: they no longer need approval.

    Names that match nothing offered are ignored, so a tool that is switched
    off or no longer exists cannot be reached through its trust entry.
    """
    names = set(current_names(trusted or []))
    if not names:
        return specs
    return [replace(spec, needs_approval=False, trusted=True) if spec.name in names and spec.needs_approval else spec for spec in specs]


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
    specs: List[ToolSpec], secret_names: Optional[List[str]] = None, *, shared_in_computer: bool = SHARED_IN_COMPUTER, selection_note: str = ""
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
            f"Files: read_file, write_file, edit_file and list_files handle files under {_file_roots(shared_in_computer)}; "
            "use them instead of heredocs or sed in the terminal; a relative path means under /workspace, so 'report.md' is /workspace/report.md. "
            + (
                "/workspace is your own project space. /shared is the user's shared folder: a directory on the host that the user "
                "and other bots see too; put anything the user should find outside the chat there, and nothing else. "
                if shared_in_computer
                else ""
            )
            +
            "Software: Ubuntu with Node and Python, and git, curl, jq, file, strings, xxd, less, nano, tree, rsync, zip/7z/xz/zstd, rg, fd, sqlite3, "
            "ImageMagick, ffmpeg, tesseract (OCR), qpdf, exiftool, pdftotext and markitdown, dig, ping and nc are already installed. "
            "So are shellcheck and ruff: a shell or Python script saved with write_file or edit_file is checked on the spot, and the "
            "result's check lists any problems; fix them before running it. For a script made another way, run `shellcheck script.sh` or "
            "`ruff check script.py` yourself before running it. "
            "computer_install_packages installs other apt packages (the shell has no sudo), e.g. build-essential for a C compiler, or pandoc; "
            "pip in a venv, npm, cargo and the like work from the shell. "
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
            "- Sharing files: share_file puts a download card for a file in the chat (source computer for a file in your computer, "
            "shared for the shared folder). Do it for anything you produce that the user should keep, then mention the file by "
            "name; do not paste large file contents into the reply instead."
        )
    if "memory" in groups:
        lines.append(
            "- Your notes: durable facts about your job, shared by all your conversations (the systems you look after, access, house "
            "rules, how things are done, lessons learned). memory_read shows them, memory_edit changes one passage, memory_write replaces "
            "the whole document. A new conversation starts with them, the user's edits appear as notes, and after a compaction the current "
            "notes come with the summary, so you do not need to read them at the start of every turn. Keep out of them anything about one "
            "piece of work, one person or one day: those belong in work notes or the project log."
        )
    if "projects" in groups:
        lines.append(
            "- Projects and work notes: a project is a named piece of work. Its work notes say where it stands (status, decisions, next "
            "steps), kept current rather than added to; its log (project_log) records what happened, dated, and stays out of your "
            "context until a message matches it. A conversation is in one project or none; without one, the work notes are the "
            "conversation's own. projects_list shows the projects, project_join moves this conversation into one, project_create starts "
            "one. When the work notes change in another conversation of the same project you are told, so read them before relying on them."
        )
    if "skills" in groups:
        lines.append(
            "- Skills: instructions for kinds of tasks, listed below by name and description. Before a task one fits, skill_read it and "
            "follow it; a skill's scripts are in its folder in your computer. Keep what you learn as a skill of your own with skill_save: "
            "the user can show you how to do something in your computer's browser (their clicks and typing arrive as a recorded list of "
            "steps with screens), and you write it up so you can do it again, adapting when a page has changed."
        )
    if "todo" in groups:
        lines.append(
            "- Your to-do list: the steps of the job you are on, kept across tool rounds and compactions. For any job with several "
            "steps, todo_add the steps first, mark each doing when you start it and done when it is finished (todo_update), and add "
            "steps you discover. Every todo result shows the whole list, the user sees and can edit it, and after a compaction the "
            "current list comes with the summary. Keep items short; it is a checklist, not notes."
        )
    if "orchestration" in groups:
        lines.append(
            "- Other bots: you are an orchestrator. bots_list and bots_status show what the user's bots are doing; bots_create makes a "
            "new one for work nobody covers; bots_send gives a bot a complete task and (with wait_seconds) brings its reply back; "
            "bots_reply collects a reply later. Delegate specialist work rather than doing it yourself, brief fully, and report to "
            "the user what was delegated, what came back, and what waits on them (a bot stopped at an approval or a question needs "
            "the user in that bot's chat)."
        )
    if "questions" in groups:
        lines.append(
            "- Questions to the user: ask_user shows a card and waits for the answer, so the user sees that you need them. Use it for "
            "decisions that are theirs (which of several ways to proceed, whether to go further than asked) and for missing "
            "information you cannot find; give short options when there are natural ones. Do not ask in a scheduled run unless "
            "you cannot continue without the answer, since nobody may be watching, and do not ask things you can look up."
        )
    if "help" in groups:
        lines.append(
            "- Cubicle's help: when the user asks how to do something in Cubicle or what a part of it does, cubicle_help looks it up "
            "in the app's own pages and says how you are set up; answer from it rather than from memory, link the page, and include "
            "a screenshot it offers when it shows what you describe."
        )
    if "schedule" in groups:
        lines.append(
            "- Schedules: schedule_create runs a prompt for you later in your Scheduled conversation, once or on a cadence, after the user "
            "approves it; schedule_list shows them with the current time and timezone; schedule_update changes one; schedule_delete removes "
            "one. Use them when the user asks to be reminded, to check something later or to repeat a job. Write the prompt as a complete "
            "message to your future self: the run starts a fresh turn in which only your notes carry over. To watch something often (a "
            "service's health, a new image version, a disk filling up), give the schedule a check_script so you are only woken when there "
            "is something to act on: it prints nothing when all is well, prints what is wrong or new otherwise, and never prints what "
            "changes every run (times, counters), since an unchanged report is not given twice. Keep state it compares against in a file "
            "in your home directory, and try the script with the terminal before scheduling it."
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
        if "composio" in toolkits:
            lines.append(
                "- Composio itself: composio_search_tools finds apps and their tools for a task (read-only; results include Composio's own "
                "skills for the task). composio_execute_tool runs any tool of a connected app by slug, after approval; prefer the app's "
                "own tools when they are offered."
            )
    if "mcp" in groups:
        lines.append(
            "- MCP servers in your computer: with mcp_add_server scope computer you can run a tool server next to your files, installing it "
            "first with your terminal (npm, pip, uv) if needed; it is yours alone and stops with the computer. "
        )
        lines.append(
            "- MCP servers: tool servers the user runs or points at; their tools are offered like a connected app's, named <server>_<tool>. "
            "When the user asks to add one, call mcp_add_server with the command (stdio) or URL (http) they give you; for an API key, "
            "tell them to add it as an app secret and reference it as $NAME, never paste a key into the chat. mcp_list_servers and "
            "mcp_test_server show what is configured and whether it works."
        )
    if "connectors" in groups:
        lines.append(
            "- Connecting apps: when the user asks to connect an app, call connector_connect with its toolkit slug (after approval it "
            "returns a sign-in link): give the user that link as a Markdown link and stop. Once they say they have signed in, "
            "connector_status confirms it, and the app's own tools are offered from your next turn."
        )
    if selection_note:
        lines.append("- " + selection_note)
    approval = sorted(spec.name for spec in specs if spec.needs_approval)
    if approval:
        lines.append("Tools that need approval: " + ", ".join(approval) + ".")
    trusted = sorted(spec.name for spec in specs if spec.trusted)
    if trusted:
        lines.append(
            "Tools the user has trusted for you, which run without asking (use them with the same care as if they asked): "
            + ", ".join(trusted) + "."
        )
    return "\n".join(lines)


# ---- turning a model call into a gateway invocation ---------------------------


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60} min" if seconds >= 60 and seconds % 60 == 0 else f"{seconds} s"


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
    path = _string(arguments, "path", required=True, max_len=512)
    try:
        return check_file_path(path)
    except ComputerProviderError as exc:
        raise ToolCallError(str(exc)) from exc


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


# The project tools: action name, and each argument with (required, max length).
PROJECT_CALLS: Dict[str, Any] = {
    "work_notes_read": ("work_notes.read", {"project": (False, 200)}),
    "work_notes_write": ("work_notes.write", {"text": (True, settings.BOT_WORK_NOTES_MAX_CHARS + 2000), "project": (False, 200)}),
    "work_notes_edit": ("work_notes.edit", {"old_text": (True, settings.BOT_WORK_NOTES_MAX_CHARS), "new_text": (False, settings.BOT_WORK_NOTES_MAX_CHARS), "project": (False, 200)}),
    "project_log": ("project_log.add", {"entry": (True, 6000), "project": (False, 200)}),
    "project_log_read": ("project_log.read", {"query": (False, 1000), "limit": (False, 10), "project": (False, 200)}),
    "projects_list": ("projects.list", {}),
    "project_create": ("projects.create", {"name": (True, 200), "work_notes": (False, settings.BOT_WORK_NOTES_MAX_CHARS + 2000)}),
    "project_join": ("projects.join", {"name": (False, 200)}),
}

# Actions that belong to the conversation a call comes from, not just the bot.
THREAD_ACTIONS = {
    "todo.list", "todo.add", "todo.update", "memory.read", "memory.write", "memory.edit", "user.ask",
    "projects.list", "projects.create", "projects.join", "work_notes.read", "work_notes.write", "work_notes.edit",
    "project_log.add", "project_log.read", "bots.send", "help.search",
}


def build_invocation(
    name: str,
    arguments: Dict[str, Any],
    bot_id: str,
    specs: Optional[List[ToolSpec]] = None,
    thread_id: Optional[str] = None,
) -> Union[WorkspaceToolCall, ActionInvocation]:
    """Validate a model tool call and build the gateway input for it.

    `thread_id` is the conversation the call comes from (the bot's id when
    not given): the to-do list and questions are the conversation's, and a
    change to the notes is announced in the bot's other conversations.
    """
    invocation = _build_invocation(name, arguments, bot_id, specs)
    if isinstance(invocation, ActionInvocation) and invocation.name in THREAD_ACTIONS:
        invocation = replace(invocation, arguments={**invocation.arguments, "thread_id": thread_id or bot_id})
    return invocation


def _schedule_cadence(arguments: Dict[str, Any], *, required: bool) -> Optional[Dict[str, Any]]:
    """The cadence in a schedule tool's flat arguments, checked; None when none is given and none is required."""
    kind = arguments.get("kind")
    if not kind and not required:
        return None
    cadence: Dict[str, Any] = {"kind": _string(arguments, "kind", required=True, max_len=10).lower()}
    for key in ("at", "in_minutes", "minutes", "time", "days"):
        if arguments.get(key) not in (None, "", []):
            cadence[key] = arguments.get(key)
    try:
        return clean_cadence(cadence)
    except ScheduleError as exc:
        raise ToolCallError(str(exc)) from exc


def _schedule_check(arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        return clean_check({"script": arguments.get("check_script") or "", "timeout_seconds": arguments.get("check_timeout_seconds")})
    except ScheduleError as exc:
        raise ToolCallError(str(exc)) from exc


def _check_preview(check: Optional[Dict[str, Any]]) -> str:
    if not check:
        return ""
    script = check["script"] if len(check["script"]) <= 600 else check["script"][:600] + "..."
    return f" · check (up to {check['timeout_seconds']}s), run without asking each time:\n{script}"


def _build_invocation(
    name: str,
    arguments: Dict[str, Any],
    bot_id: str,
    specs: Optional[List[ToolSpec]] = None,
) -> Union[WorkspaceToolCall, ActionInvocation]:
    name = current_name(name)  # a former name still works
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
        args: Dict[str, Any] = {"command": command}
        preview = f"Run in the computer for {bot_id}"
        if arguments.get("timeout_seconds") not in (None, ""):
            try:
                limit = check_timeout(arguments.get("timeout_seconds"))
            except ComputerProviderError as exc:
                raise ToolCallError(str(exc)) from exc
            args["timeout_seconds"] = int(limit)
            if limit != float(settings.COMPUTER_DOCKER_COMMAND_TIMEOUT):
                preview += f" (up to {_duration(limit)})"
        if arguments.get("background") in (True, "true", "True", 1):
            args["background"] = True
            preview = f"Start a background job in the computer for {bot_id}"
            if arguments.get("notify") in (False, "false", "False", 0):
                args["notify"] = False
        return _computer_invocation("terminal_execute", bot_id, args, f"{preview}: {command[:120]}")
    if name == "read_file":
        path = _computer_file_path(arguments)
        return _computer_invocation("files_read", bot_id, {"path": path}, f"Read {path} from the computer for {bot_id}")
    if name == "write_file":
        path = _computer_file_path(arguments)
        content = _string(arguments, "content", required=True, max_len=FILE_CONTENT_MAX_CHARS)
        return _computer_invocation(
            "files_write", bot_id, {"path": path, "content": content}, f"Write {len(content)} characters to {path} in the computer for {bot_id}"
        )
    if name == "edit_file":
        path = _computer_file_path(arguments)
        old_text = _string(arguments, "old_text", required=True, max_len=FILE_CONTENT_MAX_CHARS)
        new_text = _string(arguments, "new_text", default="", max_len=FILE_CONTENT_MAX_CHARS)
        return _computer_invocation(
            "files_edit",
            bot_id,
            {"path": path, "old_text": old_text, "new_text": new_text},
            f"Edit {path} in the computer for {bot_id} ({len(old_text)} characters become {len(new_text)})",
        )
    if name == "list_files":
        path = _string(arguments, "path", default="/workspace", max_len=512)
        try:
            path = check_file_path(path or "/workspace")
        except ComputerProviderError as exc:
            raise ToolCallError(str(exc)) from exc
        return _computer_invocation("files_list", bot_id, {"path": path}, f"List computer files for {bot_id} at {path}")
    if name == "computer_install_packages":
        try:
            names = check_package_names(arguments.get("packages"))
        except ComputerProviderError as exc:
            raise ToolCallError(str(exc)) from exc
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
        source = normalize_source(_string(arguments, "source", required=True, max_len=20))
        if source not in {"workspace", "computer"}:
            raise ToolCallError("'source' must be computer or shared.")
        path = _string(arguments, "path", required=True, max_len=1000).strip()
        if not path:
            raise ToolCallError("'path' is required.")
        where = "the shared folder" if source == "workspace" else f"the computer for {bot_id}"
        return ActionInvocation(
            name="files.share",
            arguments={"source": source, "path": path},
            target={"bot_id": bot_id, "source": source},
            preview=f"Share {path} from {where} with the user",
        )

    if name == "todo_list":
        return ActionInvocation(name="todo.list", arguments={"bot_id": bot_id}, target={"bot_id": bot_id}, preview=f"Read the to-do list of {bot_id}")
    if name == "bots_list":
        task = _string(arguments, "task", max_len=2000).strip()
        return ActionInvocation(name="bots.list", arguments={"task": task} if task else {}, target={"bot_id": bot_id}, preview="List the user's bots" + (" ranked for a task" if task else ""))
    if name == "bots_status":
        other = _string(arguments, "bot_id", required=True, max_len=40)
        return ActionInvocation(name="bots.status", arguments={"bot_id": other}, target={"bot_id": bot_id}, preview=f"Look at bot {other}")
    if name == "bots_create":
        new_name = _string(arguments, "name", required=True, max_len=80).strip()
        role = _string(arguments, "role", required=True, max_len=200).strip()
        prompt = _string(arguments, "system_prompt", required=True, max_len=20000).strip()
        if not new_name or not role or not prompt:
            raise ToolCallError("'name', 'role' and 'system_prompt' are required.")
        return ActionInvocation(
            name="bots.create",
            arguments={
                "name": new_name,
                "role": role,
                "system_prompt": prompt,
                "description": _string(arguments, "description", default="", max_len=300),
                "avatar": _string(arguments, "avatar", default="", max_len=16),
                "model": _string(arguments, "model", default="", max_len=200),
            },
            target={"bot_id": bot_id},
            preview=f"Create a bot named {new_name} ({role})",
        )
    if name == "bots_send":
        other = _string(arguments, "bot_id", required=True, max_len=40)
        message = _string(arguments, "message", required=True, max_len=20000).strip()
        if not message:
            raise ToolCallError("'message' is required.")
        wait = arguments.get("wait_seconds", 600)
        if not isinstance(wait, (int, float)) or isinstance(wait, bool) or wait < 0:
            raise ToolCallError("'wait_seconds' must be a number of seconds, 0 or more.")
        return ActionInvocation(
            name="bots.send",
            arguments={"bot_id": other, "message": message, "wait_seconds": min(float(wait), 1800.0)},
            target={"bot_id": bot_id, "to": other},
            preview=f"Send bot {other} a task: {message[:140]}",
        )
    if name == "bots_reply":
        other = _string(arguments, "bot_id", required=True, max_len=40)
        return ActionInvocation(
            name="bots.reply",
            arguments={"bot_id": other, "message_id": _string(arguments, "message_id", default="", max_len=40)},
            target={"bot_id": bot_id},
            preview=f"Collect the reply of bot {other}",
        )
    if name == "ask_user":
        question = _string(arguments, "question", required=True, max_len=2000).strip()
        if not question:
            raise ToolCallError("'question' is required.")
        options = arguments.get("options")
        if options is None:
            options = []
        if isinstance(options, str):
            options = [options]
        if not isinstance(options, list) or not all(isinstance(o, str) and o.strip() for o in options):
            raise ToolCallError("'options' must be a list of short non-empty strings.")
        options = [o.strip()[:200] for o in options]
        if len(options) == 1:
            raise ToolCallError("Offer at least two options, or none for a free-text answer.")
        if len(options) > 6:
            raise ToolCallError("At most 6 options can be offered.")
        allow_other = arguments.get("allow_other", True)
        if not isinstance(allow_other, bool):
            raise ToolCallError("'allow_other' must be true or false.")
        return ActionInvocation(
            name="user.ask",
            arguments={"bot_id": bot_id, "question": question, "options": options, "allow_other": allow_other or not options},
            target={"bot_id": bot_id},
            preview=f"Ask the user: {question[:160]}",
        )
    if name == "todo_add":
        items = arguments.get("items")
        if isinstance(items, str):
            items = [items]
        if not isinstance(items, list) or not items or not all(isinstance(i, str) and i.strip() for i in items):
            raise ToolCallError("'items' must be a list of one or more item texts.")
        if len(items) > settings.BOT_TODO_MAX_ITEMS:
            raise ToolCallError(f"At most {settings.BOT_TODO_MAX_ITEMS} items can be added.")
        return ActionInvocation(
            name="todo.add",
            arguments={"bot_id": bot_id, "items": [i.strip() for i in items]},
            target={"bot_id": bot_id},
            preview=f"Add {len(items)} item(s) to the to-do list of {bot_id}",
        )
    if name == "todo_update":
        item_id = _string(arguments, "id", required=True, max_len=20)
        state = _string(arguments, "state", default="", max_len=20).lower()
        if state and state not in TODO_STATES:
            raise ToolCallError("'state' must be open, doing or done.")
        text = _string(arguments, "text", default="", max_len=1000)
        remove = bool(arguments.get("remove"))
        if not remove and not state and not text:
            raise ToolCallError("Give a new state, a new text, or remove=true.")
        changes: Dict[str, Any] = {"bot_id": bot_id, "id": item_id, "remove": remove}
        if state:
            changes["state"] = state
        if text:
            changes["text"] = text
        what = "Remove" if remove else "Update"
        return ActionInvocation(name="todo.update", arguments=changes, target={"bot_id": bot_id}, preview=f"{what} to-do item {item_id} of {bot_id}")
    if name == "schedule_list":
        return ActionInvocation(name="schedule.list", arguments={"bot_id": bot_id}, target={"bot_id": bot_id}, preview=f"Read the schedules of {bot_id}")
    if name == "schedule_create":
        title = _string(arguments, "name", required=True, max_len=80)
        prompt = _string(arguments, "prompt", required=True, max_len=4000)
        cleaned = _schedule_cadence(arguments, required=True)
        check = _schedule_check(arguments)
        return ActionInvocation(
            name="schedule.create",
            arguments={"bot_id": bot_id, "name": title, "prompt": prompt, "cadence": cleaned, "check": check},
            target={"bot_id": bot_id},
            preview=f"Schedule '{title}' {describe_cadence(cleaned)} for {bot_id}: {prompt[:120]}" + _check_preview(check),
        )
    if name == "schedule_update":
        schedule_id = _string(arguments, "id", required=True, max_len=40)
        changes: Dict[str, Any] = {}
        described: List[str] = []
        if arguments.get("name") not in (None, ""):
            changes["name"] = _string(arguments, "name", max_len=80)
            described.append(f"name '{changes['name']}'")
        if arguments.get("prompt") not in (None, ""):
            changes["prompt"] = _string(arguments, "prompt", max_len=4000)
            described.append(f"prompt: {changes['prompt'][:120]}")
        cadence = _schedule_cadence(arguments, required=False)
        if cadence is not None:
            changes["cadence"] = cadence
            described.append(describe_cadence(cadence))
        if "check_script" in arguments:
            changes["check"] = _schedule_check(arguments)
            described.append("no check" if changes["check"] is None else _check_preview(changes["check"]).lstrip(" ·"))
        if isinstance(arguments.get("enabled"), bool):
            changes["enabled"] = arguments["enabled"]
            described.append("enabled" if arguments["enabled"] else "paused")
        if not changes:
            raise ToolCallError("Give at least one thing to change.")
        return ActionInvocation(
            name="schedule.update",
            arguments={"bot_id": bot_id, "id": schedule_id, **changes},
            target={"bot_id": bot_id},
            preview=f"Change schedule {schedule_id} of {bot_id}: " + "; ".join(described),
        )
    if name == "schedule_delete":
        schedule_id = _string(arguments, "id", required=True, max_len=40)
        return ActionInvocation(name="schedule.delete", arguments={"bot_id": bot_id, "id": schedule_id}, target={"bot_id": bot_id}, preview=f"Delete schedule {schedule_id} of {bot_id}")
    if name == "mcp_list_servers":
        return ActionInvocation(name="mcp.servers_list", arguments={}, target={"bot_id": bot_id}, preview="List MCP servers")
    if name == "__mcp_list_servers_old__":
        return ActionInvocation(name="mcp.servers_list", arguments={}, target={}, preview="List the MCP servers")
    if name == "mcp_add_server":
        from app.services.mcp_service import McpError, clean_server

        scope = str(arguments.get("scope") or "computer").strip().lower()
        if scope not in ("computer", "bot", "user"):
            raise ToolCallError("'scope' must be computer, bot or user.")
        fields = {k: arguments.get(k) for k in ("name", "transport", "command", "args", "env", "url", "headers", "trust_read_only")}
        if arguments.get("tools"):
            fields.update(tool_mode="only", tool_names=arguments.get("tools"))
        if scope == "computer":
            fields["transport"] = "computer"
        elif scope == "bot":
            if fields.get("transport") not in ("http", "sse"):
                fields["transport"] = "http"
            if not fields.get("url"):
                raise ToolCallError("A bot-scope server is a url; for a command use scope 'computer'.")
        elif not fields.get("transport"):
            raise ToolCallError("A user-scope server needs 'transport': stdio, or http (or sse) for a URL.")
        try:
            record = clean_server(fields)
        except McpError as exc:
            raise ToolCallError(str(exc)) from exc
        where = f"{record['command']} {' '.join(record['args'])}".strip() if record["transport"] not in ("http", "sse") else record["url"]
        place = {"computer": "in your computer", "bot": "for you"}.get(scope, "for every bot")
        return ActionInvocation(
            name="mcp.servers_add",
            arguments={"scope": scope, "server": {k: record[k] for k in ("name", "transport", "command", "args", "env", "url", "headers", "trust_read_only", "tool_mode", "tool_names")}},
            target={"mcp_server": record["name"], "bot_id": bot_id},
            preview=f"Add MCP server '{record['name']}' {place} ({record['transport']}): {where[:120]}" + (" (trusted read-only)" if record["trust_read_only"] else "")
            + (f"; offers only {len(record['tool_names'])} of its tools" if record["tool_mode"] == "only" else ""),
        )
    if name == "mcp_test_server":
        server_id = _string(arguments, "id", required=True, max_len=20)
        return ActionInvocation(name="mcp.servers_test", arguments={"id": server_id}, target={"mcp_server": server_id, "bot_id": bot_id}, preview=f"Test MCP server {server_id}")
    if name == "mcp_remove_server":
        server_id = _string(arguments, "id", required=True, max_len=20)
        return ActionInvocation(name="mcp.servers_remove", arguments={"id": server_id}, target={"mcp_server": server_id, "bot_id": bot_id}, preview=f"Remove MCP server {server_id}")
    if name == "connector_connect":
        toolkit = _string(arguments, "toolkit", required=True, max_len=64).lower()
        if not _TOOLKIT_SLUG.fullmatch(toolkit):
            raise ToolCallError("'toolkit' must be a Composio toolkit slug such as googledrive.")
        return ActionInvocation(
            name="connector.connect",
            arguments={"toolkit": toolkit},
            target={"toolkit": toolkit, "bot_id": bot_id},
            preview=f"Connect {toolkit} through Composio: the user gets a sign-in link",
        )
    if name == "connector_status":
        raw = arguments.get("toolkits")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or not raw:
            raise ToolCallError("'toolkits' must list one or more toolkit slugs.")
        slugs = []
        for item in raw[:20]:
            slug = str(item).strip().lower()
            if not _TOOLKIT_SLUG.fullmatch(slug):
                raise ToolCallError(f"{str(item)[:40]!r} is not a toolkit slug.")
            if slug not in slugs:
                slugs.append(slug)
        return ActionInvocation(
            name="connector.status",
            arguments={"toolkits": slugs},
            target={"toolkits": slugs, "bot_id": bot_id},
            preview=f"Check connections: {', '.join(slugs)}",
        )
    if name == "cubicle_help":
        question = _string(arguments, "question", required=True, max_len=1000).strip()
        if not question:
            raise ToolCallError("'question' is required.")
        return ActionInvocation(name="help.search", arguments={"bot_id": bot_id, "question": question}, target={"bot_id": bot_id}, preview=f"Look up in Cubicle's help: {question[:120]}")
    if name in PROJECT_CALLS:
        action, fields = PROJECT_CALLS[name]
        payload: Dict[str, Any] = {"bot_id": bot_id}
        for field, (required, max_len) in fields.items():
            value = _string(arguments, field, required=required, default="", max_len=max_len) if field != "limit" else arguments.get("limit")
            if value not in (None, ""):
                payload[field] = value
        if name == "project_join" and "name" not in payload:
            payload["name"] = ""
        what = payload.get("name") or payload.get("project") or ""
        return ActionInvocation(name=action, arguments=payload, target={"bot_id": bot_id}, preview=f"{name.replace('_', ' ')}{f' ({what})' if what else ''}")
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

    # procedure_save/read: the former names, as a call from an older conversation or habit uses them.
    if name in ("skill_save", "procedure_save"):
        title = _string({**arguments, "name": arguments.get("name") or arguments.get("title")}, "name", required=True, max_len=200)
        steps = _string({**arguments, "instructions": arguments.get("instructions") or arguments.get("steps")}, "instructions", required=True, max_len=20_000)
        description = _string(arguments, "description", max_len=1000) or ""
        return ActionInvocation(
            name="skill.save",
            arguments={"bot_id": bot_id, "name": title, "description": description, "instructions": steps},
            target={"bot_id": bot_id},
            preview=f"Save the skill {title!r} for {bot_id} ({len(steps)} characters)",
        )
    if name in ("skill_read", "procedure_read"):
        title = _string({**arguments, "name": arguments.get("name") or arguments.get("title")}, "name", required=True, max_len=200)
        file = _string(arguments, "file", max_len=500) or ""
        return ActionInvocation(
            name="skill.read",
            arguments={"bot_id": bot_id, "name": title, **({"file": file} if file else {})},
            target={"bot_id": bot_id},
            preview=f"Read the skill {title!r}" + (f": {file}" if file else ""),
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
            return connector_invocation(spec, arguments, bot_id)

    raise ToolCallError(unknown_message(name, [s.name for s in specs or []]))


def connector_invocation(spec: ToolSpec, arguments: Dict[str, Any], bot_id: str = "") -> ActionInvocation:
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
    mcp = info.get("mcp")
    if isinstance(mcp, dict):
        return ActionInvocation(
            name="mcp.read" if info.get("read_only") else "mcp.call",
            arguments={"server": mcp.get("server"), "tool": mcp.get("tool"), "arguments": arguments},
            target={"mcp_server": mcp.get("server"), "tool": mcp.get("tool"), "bot_id": bot_id or None},
            preview=preview[:300],
            display_arguments=arguments,
        )
    action_name = "connector.composio_read" if info.get("read_only") else "connector.composio_action"
    gateway_arguments: Dict[str, Any] = {"tool": info["slug"], "arguments": arguments}
    if info.get("version"):
        gateway_arguments["version"] = info["version"]
    return ActionInvocation(
        name=action_name,
        arguments=gateway_arguments,
        # The bot decides, on a teammate's turn, whose connection runs it.
        target={"connector": info.get("toolkit"), "tool": info["slug"], "bot_id": bot_id},
        preview=preview[:300],
        display_arguments=arguments,
    )
