"""Tool names the model may use for the built-in tools, and how they resolve.

The file tools were called computer_read_file, computer_write_file,
computer_edit_file and computer_files_list until 2026-10. Models trained on
coding agents reach for read_file, write_file and edit_file by habit (Qwen
sent write_file alongside the real call), so those became the names. The
old names stay valid: stored settings (trusted tools, per-bot tool
switches) and old transcripts carry them, and they resolve to the new ones.

A few names models invent for the same jobs resolve too, but only to a
tool that is on offer; anything else gets an error naming the closest
real tools, so the model corrects itself on the next round.

This module imports nothing from the app, so storage can use it.
"""

from __future__ import annotations

import difflib
from typing import Any, Dict, Iterable, List, Optional

# Former names of built-in tools -> current names.
RENAMED: Dict[str, str] = {
    "computer_read_file": "read_file",
    "computer_write_file": "write_file",
    "computer_edit_file": "edit_file",
    "computer_files_list": "list_files",
}

# Names models use by habit for the same jobs (other agents' tool names).
HABITS: Dict[str, str] = {
    "view_file": "read_file",
    "open_file": "read_file",
    "cat_file": "read_file",
    "create_file": "write_file",
    "write_to_file": "write_file",
    "save_file": "write_file",
    "replace_in_file": "edit_file",
    "str_replace": "edit_file",
    "str_replace_editor": "edit_file",
    "edit": "edit_file",
    "list_directory": "list_files",
    "list_dir": "list_files",
    "ls": "list_files",
    "run_command": "computer_terminal_execute",
    "run_shell_command": "computer_terminal_execute",
    "execute_command": "computer_terminal_execute",
    "run_terminal_cmd": "computer_terminal_execute",
    "shell": "computer_terminal_execute",
    "bash": "computer_terminal_execute",
    "terminal": "computer_terminal_execute",
    "screenshot": "computer_screenshot",
    "take_screenshot": "computer_screenshot",
}


def current_name(name: Any) -> str:
    """A stored or replayed tool name as it is called today."""
    text = str(name or "")
    return RENAMED.get(text, text)


def current_names(names: Iterable[Any]) -> List[str]:
    """A stored list of tool names (trusted tools), renamed and without duplicates."""
    out: List[str] = []
    for name in names or []:
        if not isinstance(name, str):
            continue
        renamed = current_name(name)
        if renamed not in out:
            out.append(renamed)
    return out


def migrate_bot(bot: Dict[str, Any]) -> Dict[str, Any]:
    """A bot record with former tool names in its settings replaced (read-time; saved on the next write)."""
    trusted = bot.get("trusted_tools")
    if isinstance(trusted, list) and any(isinstance(n, str) and n in RENAMED for n in trusted):
        bot["trusted_tools"] = current_names(trusted)
    settings = bot.get("tool_settings")
    if isinstance(settings, dict) and isinstance(settings.get("tools"), dict) and any(k in RENAMED for k in settings["tools"]):
        settings["tools"] = {current_name(k): v for k, v in settings["tools"].items()}
    return bot


def resolve(name: Any, offered: Iterable[str], callable_names: Optional[Iterable[str]] = None) -> str:
    """The tool a call means.

    `offered` are the tools sent this round; `callable_names` adds tools the
    bot has but the round did not list (app tools held back by tool
    selection). An unknown name comes back unchanged.
    """
    text = str(name or "")
    offered_set = set(offered)
    if text in offered_set:
        return text
    reachable = offered_set | set(callable_names or ())
    for table in (RENAMED, HABITS):
        target = table.get(text) or table.get(text.lower())
        if target and target in reachable:
            return target
    if text in reachable:
        return text
    return text


def unknown_message(name: Any, offered: Iterable[str]) -> str:
    """The error for a tool that does not exist, naming the closest real ones."""
    text = str(name or "")
    names = sorted(set(offered))
    # The name as given, and without a prefix models add or drop (computer_write
    # means write_file); matches for the bare name come first.
    variants = [text]
    for prefix in ("computer_", "functions.", "sandbox_", "tool_"):
        if text.startswith(prefix) and len(text) > len(prefix):
            variants.insert(0, text[len(prefix):])
    close: List[str] = []
    for variant in variants:
        for match in difflib.get_close_matches(variant, names, n=3, cutoff=0.55):
            if match not in close:
                close.append(match)
    close = close[:3]
    if not close:
        # A part of the name (write, file, search...) may still point the way.
        parts = {p for p in text.lower().replace("-", "_").split("_") if len(p) > 2}
        close = [n for n in names if parts & set(n.split("_"))][:3]
    if close:
        return f"Unknown tool: {text}. There is no tool by that name; did you mean {' or '.join(close)}? Call it by that exact name."
    return f"Unknown tool: {text}. Call only the tools listed in this request, by their exact names."
