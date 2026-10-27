"""A bot's conversations share its computer, so what changes it takes turns.

Two conversations of the same bot can reply at the same time, but a
terminal command, a file write or edit, a package install, typing or
clicking, and browser navigation each wait while another conversation's is
running on that computer, so they cannot trip over each other. Reading
files, listing them and screenshots never wait: they cannot conflict. A
background job holds the computer only while it is being started.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Dict, Optional, Tuple

TAKES_TURNS = frozenset({
    "computer.terminal_execute",
    "computer.files_write",
    "computer.files_edit",
    "computer.install_packages",
    "computer.send_input",
    "computer.browser_navigate",
})

# (event loop, bot id) -> lock: asyncio locks belong to one loop.
_locks: Dict[Tuple[int, str], asyncio.Lock] = {}
# bot id -> who has the computer now: {"thread_id", "action"}
_holders: Dict[str, Dict[str, Any]] = {}


def takes_turns(action_name: str) -> bool:
    return action_name in TAKES_TURNS


def holder(bot_id: str) -> Optional[Dict[str, Any]]:
    return _holders.get(bot_id)


@asynccontextmanager
async def computer_turn(bot_id: str, thread_id: str, action: str, on_wait: Optional[Callable[[Dict[str, Any]], None]] = None) -> AsyncIterator[None]:
    """Hold the bot's computer for one action; `on_wait` is told who has it when this one must wait."""
    key = (id(asyncio.get_running_loop()), bot_id)
    lock = _locks.setdefault(key, asyncio.Lock())
    current = _holders.get(bot_id)
    if lock.locked() and on_wait is not None and current is not None:
        on_wait(current)
    async with lock:
        _holders[bot_id] = {"thread_id": thread_id, "action": action}
        try:
            yield
        finally:
            _holders.pop(bot_id, None)
