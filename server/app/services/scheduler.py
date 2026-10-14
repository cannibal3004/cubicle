"""The scheduler: one background task that runs due schedules.

Every tick it asks schedule_service for the schedules whose next run has
passed and starts each as a turn in its bot's chat, through a callback the
chat router registers at startup (the router owns turn setup; this module
must not import it). A bot whose reply is still in progress is skipped for
that run and the schedule moves on to the next time. Nothing is caught up
after a restart: the next run is computed from the cadence, not from what
was missed.

Each run is its own task so a slow turn never delays another bot's
schedule, and every failure is recorded on the schedule rather than raised.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional, Set

from app.config import settings
from app.services import schedule_service
from app.services.turn_manager import TurnBusyError, turn_manager

logger = logging.getLogger(__name__)

# (bot_id, message) -> the started Turn, or raises TurnBusyError.
RunTurn = Callable[[str, Dict[str, Any]], Awaitable[Any]]


class Scheduler:
    def __init__(self, tick_seconds: Optional[float] = None):
        self.tick_seconds = tick_seconds or settings.SCHEDULE_TICK_SECONDS
        self._run_turn: Optional[RunTurn] = None
        self._task: Optional[asyncio.Task] = None
        self._runs: Set[asyncio.Task] = set()
        self._running_ids: Set[str] = set()

    def start(self, run_turn: RunTurn) -> None:
        self._run_turn = run_turn
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._loop())
            logger.info("Scheduler started (every %ss, timezone %s)", self.tick_seconds, settings.SCHEDULE_TIMEZONE or "UTC")

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
                await self.tick()
            except Exception as exc:  # The loop must outlive any one bad tick.
                logger.exception("Scheduler tick failed: %s", exc)
            await asyncio.sleep(self.tick_seconds)

    async def tick(self, now=None) -> int:
        """Start every due schedule; returns how many were started or skipped."""
        if self._run_turn is None:
            return 0
        started = 0
        for record in schedule_service.due_schedules(now):
            if record["id"] in self._running_ids:
                continue  # Its previous run has not finished yet.
            started += 1
            current = turn_manager.current(record["bot_id"])
            if current is not None and not current.finished:
                schedule_service.record_run(record["id"], "skipped", "a reply was still in progress", now=now)
                logger.info("Schedule %s (%s) skipped: bot %s busy", record["id"], record["name"], record["bot_id"])
                continue
            task = asyncio.get_running_loop().create_task(self._run(record, now))
            self._runs.add(task)
            task.add_done_callback(self._runs.discard)
        return started

    async def run_now(self, record: Dict[str, Any]) -> bool:
        """Start a schedule's run immediately (the editor's Run now). False when the bot is busy."""
        if self._run_turn is None or record["id"] in self._running_ids:
            return False
        current = turn_manager.current(record["bot_id"])
        if current is not None and not current.finished:
            return False
        task = asyncio.get_running_loop().create_task(self._run(record))
        self._runs.add(task)
        task.add_done_callback(self._runs.discard)
        return True

    async def _run(self, record: Dict[str, Any], now=None) -> None:
        schedule_id = record["id"]
        self._running_ids.add(schedule_id)
        try:
            message = schedule_service.scheduled_message(record, message_id=f"msg-{uuid.uuid4().hex[:6]}", now=now)
            try:
                turn = await self._run_turn(record["bot_id"], message)
            except TurnBusyError:
                schedule_service.record_run(schedule_id, "skipped", "a reply was still in progress", now=now)
                return
            except Exception as exc:
                logger.exception("Schedule %s could not start: %s", schedule_id, exc)
                schedule_service.record_run(schedule_id, "failed", f"could not start: {exc}", now=now)
                return
            # The schedule moves on as soon as the run starts (a long turn
            # must not delay the next occurrence); the outcome lands after.
            schedule_service.record_run(schedule_id, "running", "", now=now)
            logger.info("Schedule %s (%s) started turn %s for bot %s", schedule_id, record["name"], getattr(turn, "turn_id", "?"), record["bot_id"])
            task = getattr(turn, "task", None)
            if task is not None:
                try:
                    await task
                except Exception:
                    pass  # The turn records its own failure.
            status = getattr(turn, "status", "completed")
            note = getattr(turn, "error", None) or ""
            if getattr(turn, "pending_approval", None):
                status, note = "needed_approval", "stopped at an approval nobody answered"
            self._finish(schedule_id, status, note)
        finally:
            self._running_ids.discard(schedule_id)

    def _finish(self, schedule_id: str, status: str, note: str) -> None:
        record = schedule_service._storage(None).get_schedule(schedule_id)
        if record is None:
            return  # Deleted while it ran.
        record["last_status"] = status
        record["last_note"] = (note or "")[: schedule_service.MAX_NOTE_CHARS]
        schedule_service._storage(None).save_schedule(record)


scheduler = Scheduler()
