"""Background jobs a bot is waiting on: when one ends, the conversation that started it is told.

A terminal command run with background: true (and notify, the default) returns at once; the
computer writes the job's output to a log and, when the command ends, its exit code to a file
next to it, in the sandbox user's home. The job is watched here: every few seconds the exit file
is looked for (read straight from the host when the computer's files are bind mounts, else through
the computer). When it appears, a message goes to the conversation the job was started from, with
the command, its exit code, how long it ran and the end of its log. It starts a turn at once when
the bot is idle there, or right after the reply in progress ends; several jobs that end together
make one message. A job whose computer stopped or was reset first, or that was killed, is reported
as having ended without an exit code; one still running after a week stops being watched.

The watches are kept in DATA_DIR/job-watches.json, so an API restart does not lose them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

POLL_SECONDS = 5.0
# How often to ask the computer whether a job without an exit file is still alive (a killed job
# never writes one).
ALIVE_CHECK_SECONDS = 60.0
MAX_WATCH_SECONDS = 7 * 24 * 3600
MAX_WATCHES_PER_BOT = 20
LOG_TAIL_LINES = 40
LOG_TAIL_CHARS = 4000

StartTurn = Callable[[str, Dict[str, Any], Optional[str]], Awaitable[Any]]


def _store_path() -> Path:
    return Path(settings.DATA_DIR) / "job-watches.json"


def _load() -> List[Dict[str, Any]]:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [w for w in data if isinstance(w, dict) and w.get("job_id")] if isinstance(data, list) else []


def _save(watches: List[Dict[str, Any]]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(watches, indent=1), encoding="utf-8")
    temporary.replace(path)


def _duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds} s" if seconds else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


class JobWatcher:
    def __init__(self) -> None:
        self._start_turn: Optional[StartTurn] = None
        self._task: Optional["asyncio.Task[None]"] = None
        self._lock = asyncio.Lock()

    # ---- registering ------------------------------------------------------------------

    def watch(self, *, owner_id: str, bot_id: str, thread_id: str, computer_id: str, result: Dict[str, Any], command: str) -> Dict[str, Any]:
        """Start watching a job the terminal just started; what the bot is told about it."""
        watches = _load()
        if sum(1 for w in watches if w.get("bot_id") == bot_id and not w.get("ended")) >= MAX_WATCHES_PER_BOT:
            return {"notify": f"Not watched: you already have {MAX_WATCHES_PER_BOT} jobs being watched. Check this one with its check command."}
        watches.append({
            "job_id": str(result.get("job_id")),
            "pid": result.get("pid"),
            "log": result.get("log"),
            "exit_file": result.get("exit_file"),
            "command": " ".join(str(command).split())[:300],
            "owner_id": owner_id,
            "bot_id": bot_id,
            "thread_id": thread_id,
            "computer_id": computer_id,
            "started_at": time.time(),
            "checked_alive_at": time.time(),
        })
        _save(watches)
        return {"notify": "You will get a message in this conversation when it ends, with its exit code and the end of its log; you can go on with other work meanwhile."}

    def pending(self, thread_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return [w for w in _load() if thread_id is None or w.get("thread_id") == thread_id]

    # ---- the loop ---------------------------------------------------------------------

    def start(self, start_turn: StartTurn) -> None:
        self._start_turn = start_turn
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one bad pass must not end the watching
                logger.warning("Job watch pass failed: %s", exc)
            await asyncio.sleep(POLL_SECONDS)

    async def poll(self) -> None:
        """One pass: note the jobs that ended, then deliver what can be delivered."""
        async with self._lock:
            watches = _load()
            if not watches:
                return
            changed = False
            for watch in watches:
                if watch.get("ended"):
                    continue
                checked = watch.get("checked_alive_at")
                outcome = await self._outcome(watch)
                if outcome is not None:
                    watch.update(outcome, ended=time.time())
                changed = changed or outcome is not None or watch.get("checked_alive_at") != checked
            delivered = await self._deliver([w for w in watches if w.get("ended")])
            if delivered:
                watches = [w for w in watches if w["job_id"] not in delivered]
                changed = True
            if changed:
                _save(watches)

    # ---- finding out how a job ended -----------------------------------------------------

    @staticmethod
    def _home_path(watch: Dict[str, Any], container_path: Optional[str]) -> Optional[Path]:
        """Where a file of the job is on this side, when the computer's files are bind mounts."""
        if not container_path or settings.COMPUTER_PROVIDER != "docker" or settings.COMPUTER_DOCKER_WORKSPACE_MODE != "bind":
            return None
        home = Path(settings.COMPUTER_DOCKER_WORKSPACE_ROOT) / f"{watch['computer_id']}.home"
        return home / ".cubicle-jobs" / Path(container_path).name

    async def _outcome(self, watch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """How the job ended, or None while it runs."""
        exit_code, log_tail = await self._read_exit(watch)
        if exit_code is not None:
            return {"exit_code": exit_code, "log_tail": log_tail}
        if time.time() - float(watch.get("started_at") or 0) > MAX_WATCH_SECONDS:
            return {"exit_code": None, "lost": "it was still running after a week, so it is no longer watched", "log_tail": log_tail}
        if not self._computer_running(watch):
            # It may have ended just before the computer stopped.
            exit_code, log_tail = await self._read_exit(watch, allow_computer=False)
            if exit_code is not None:
                return {"exit_code": exit_code, "log_tail": log_tail}
            return {"exit_code": None, "lost": "the computer stopped (or was reset) before it ended", "log_tail": log_tail}
        if time.time() - float(watch.get("checked_alive_at") or 0) >= ALIVE_CHECK_SECONDS:
            watch["checked_alive_at"] = time.time()
            alive = await self._alive(watch)
            if alive is False:
                exit_code, log_tail = await self._read_exit(watch)
                if exit_code is not None:
                    return {"exit_code": exit_code, "log_tail": log_tail}
                return {"exit_code": None, "lost": "it was stopped (killed) before it could record an exit code", "log_tail": log_tail}
        return None

    def _computer_running(self, watch: Dict[str, Any]) -> bool:
        from app.services.computer_provider import computer_provider

        try:
            return computer_provider.describe(watch["bot_id"]).state in ("running", "paused", "starting")
        except Exception:
            return True  # unsure: keep waiting rather than report a loss

    async def _read_exit(self, watch: Dict[str, Any], allow_computer: bool = True):
        """(exit code or None, the end of the log)."""
        exit_path = self._home_path(watch, watch.get("exit_file"))
        log_path = self._home_path(watch, watch.get("log"))
        if exit_path is not None:
            try:
                code_text = exit_path.read_text(encoding="utf-8").strip() if exit_path.exists() else ""
            except OSError:
                code_text = ""
            tail = ""
            if code_text and log_path is not None:
                try:
                    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    tail = "\n".join(lines[-LOG_TAIL_LINES:])[-LOG_TAIL_CHARS:]
                except OSError:
                    tail = ""
            return (int(code_text) if code_text.lstrip("-").isdigit() else None), tail
        if not allow_computer:
            return None, ""
        # Not reachable from here: ask the computer.
        exit_file, log = watch.get("exit_file") or "", watch.get("log") or ""
        command = f"cat {shlex.quote(exit_file)} 2>/dev/null && echo '---' && tail -n {LOG_TAIL_LINES} {shlex.quote(log)} 2>/dev/null"
        output = await self._run(watch, command)
        if output is None or "---" not in output:
            return None, ""
        code_text, _, tail = output.partition("---")
        code_text = code_text.strip()
        return (int(code_text) if code_text.lstrip("-").isdigit() else None), tail.strip()[-LOG_TAIL_CHARS:]

    async def _alive(self, watch: Dict[str, Any]) -> Optional[bool]:
        pid = watch.get("pid")
        if not isinstance(pid, int):
            return None
        output = await self._run(watch, f"kill -0 -- -{pid} 2>/dev/null && echo alive || echo gone")
        if output is None:
            return None
        return "alive" in output

    @staticmethod
    async def _run(watch: Dict[str, Any], command: str) -> Optional[str]:
        from app.services.computer_provider import computer_provider
        from app.services.identity import owner_context

        try:
            with owner_context(watch.get("owner_id")):
                result = await computer_provider.terminal_execute(watch["computer_id"], command, 20)
        except Exception:
            return None
        return str(result.get("stdout") or "")

    # ---- telling the conversation ----------------------------------------------------------

    @staticmethod
    def _line(watch: Dict[str, Any]) -> str:
        ran = _duration(float(watch.get("ended") or time.time()) - float(watch.get("started_at") or 0))
        what = f"`{watch['command']}` ({watch['job_id']})"
        if watch.get("exit_code") is not None:
            status = "finished" if watch["exit_code"] == 0 else f"failed with exit code {watch['exit_code']}"
            if watch["exit_code"] == 0:
                status += " (exit code 0)"
            head = f"{what} {status} after {ran}."
        else:
            head = f"{what} ended without an exit code after about {ran}: {watch.get('lost')}."
        tail = (watch.get("log_tail") or "").strip()
        log = f" Its log: {watch.get('log')}." if watch.get("log") else ""
        if tail:
            return f"{head}{log} The end of it:\n```\n{tail}\n```"
        return f"{head}{log} It printed nothing." if watch.get("exit_code") is not None else f"{head}{log}"

    async def _deliver(self, ended: List[Dict[str, Any]]) -> List[str]:
        """Post each conversation's ended jobs as one message, where no reply is running; the job ids done."""
        from app.services import conversations, secrets_service
        from app.services.identity import owner_context
        from app.services.turn_manager import TurnBusyError, turn_manager

        if not ended or self._start_turn is None:
            return []
        done: List[str] = []
        by_thread: Dict[str, List[Dict[str, Any]]] = {}
        for watch in ended:
            by_thread.setdefault(watch["thread_id"], []).append(watch)
        for thread_id, watches in by_thread.items():
            with owner_context(watches[0].get("owner_id")):
                if conversations.get(thread_id) is None and thread_id != watches[0]["bot_id"]:
                    done.extend(w["job_id"] for w in watches)  # the conversation was deleted
                    continue
            current = turn_manager.current(thread_id)
            if current is not None and not current.finished:
                continue  # told after the reply in progress ends
            first = watches[0]
            with owner_context(first.get("owner_id")):
                try:
                    redact = secrets_service.redactor_for(first["bot_id"])
                except Exception:
                    redact = None
                lines = [self._line(w) for w in watches]
                intro = "[A background job you started has ended.]" if len(watches) == 1 else f"[{len(watches)} background jobs you started have ended.]"
                text = intro + "\n\n" + "\n\n".join(lines) + "\n\nCarry on with what you were doing, or tell the user what happened."
                if redact is not None:
                    text = redact(text)
                message = {
                    "id": f"msg-{uuid.uuid4().hex[:6]}",
                    "thread_id": thread_id,
                    "bot_id": first["bot_id"],
                    "sender": "user",
                    "text": text,
                    "created_at": datetime.now().isoformat(),
                    "item_type": "user_text",
                    "raw_payload": {"jobs": [{"id": w["job_id"], "command": w["command"], "exit_code": w.get("exit_code")} for w in watches]},
                }
                try:
                    await self._start_turn(thread_id, message, first.get("owner_id"))
                except TurnBusyError:
                    continue
                except Exception as exc:
                    logger.warning("Could not tell %s about ended jobs: %s", thread_id, exc)
                    continue
            done.extend(w["job_id"] for w in watches)
        return done


job_watcher = JobWatcher()
