"""Server-owned chat turns with replayable event streams.

A turn is the work the server does after a user message: calling the model,
running tools, waiting for approvals, persisting the reply. It used to live
inside the browser's Server-Sent-Events connection, so closing the tab or
switching views cancelled it. Here each turn runs as its own asyncio task and
appends numbered events to a buffer; any number of subscribers (the same
browser after a reload, or another machine) replay the buffer from a sequence
number and then follow live. One turn runs per thread at a time.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, Iterable, Iterator, List, Optional, Set


def _iso(timestamp: Optional[float]) -> Optional[str]:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


# The model streams a token per event, so a long reply buffers tens of thousands of them.
# A replay sends each run of them as one event (with the run's last seq, so a resume
# continues after it): the same text, without one event per token over the wire.
DELTA_TYPES = ("content.delta", "reasoning.delta")
MERGED_DELTA_MAX_CHARS = 64_000


def merged_deltas(events: Iterable[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    run: Optional[Dict[str, Any]] = None
    pieces: List[str] = []
    size = 0
    for event in events:
        if run is not None and (
            event.get("type") != run["type"] or event.get("botMsgId") != run.get("botMsgId") or size >= MERGED_DELTA_MAX_CHARS
        ):
            yield {**run, "delta": "".join(pieces)}
            run = None
        if event.get("type") not in DELTA_TYPES:
            yield event
            continue
        if run is None:
            pieces, size = [], 0
        run = event
        delta = str(event.get("delta") or "")
        pieces.append(delta)
        size += len(delta)
    if run is not None:
        yield {**run, "delta": "".join(pieces)}


class TurnBusyError(RuntimeError):
    """Raised when a thread already has a running turn."""

    def __init__(self, turn: "Turn"):
        super().__init__(f"A turn is already running for {turn.thread_id}.")
        self.turn = turn


@dataclass
class Turn:
    turn_id: str
    thread_id: str
    bot_id: str
    model: str
    created_at: float
    status: str = "running"  # running | completed | failed | cancelled
    finished_at: Optional[float] = None
    error: Optional[str] = None
    events: List[Dict[str, Any]] = field(default_factory=list)
    pending_approval: Optional[Dict[str, Any]] = None
    task: Optional["asyncio.Task[None]"] = None
    _subscribers: Set["asyncio.Queue[Optional[Dict[str, Any]]]"] = field(default_factory=set, repr=False)

    @property
    def finished(self) -> bool:
        return self.status != "running"

    @property
    def seq(self) -> int:
        return len(self.events)

    def emit(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Append an event and fan it out to live subscribers."""
        payload = dict(event)
        payload["seq"] = len(self.events) + 1
        payload["turnId"] = self.turn_id
        self.events.append(payload)
        for queue in list(self._subscribers):
            queue.put_nowait(payload)
        return payload

    def set_pending_approval(self, approval: Optional[Dict[str, Any]]) -> None:
        self.pending_approval = approval

    def finish(self, status: str, error: Optional[str] = None) -> None:
        if self.finished:
            return
        self.status = status
        self.error = error
        self.finished_at = time.time()
        self.pending_approval = None
        for queue in list(self._subscribers):
            queue.put_nowait(None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "thread_id": self.thread_id,
            "bot_id": self.bot_id,
            "model": self.model,
            "status": self.status,
            "seq": self.seq,
            "pending_approval": self.pending_approval,
            "error": self.error,
            "created_at": _iso(self.created_at),
            "finished_at": _iso(self.finished_at),
        }

    async def subscribe(self, after: int = 0) -> AsyncGenerator[Dict[str, Any], None]:
        """Yield events with seq > after: the buffered ones, then live ones."""
        queue: "asyncio.Queue[Optional[Dict[str, Any]]]" = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            last = max(0, int(after or 0))
            # Register first, replay second, so nothing emitted in between is lost.
            for event in merged_deltas(e for e in list(self.events) if e["seq"] > last):
                last = event["seq"]
                yield event
            if self.finished:
                return
            while True:
                event = await queue.get()
                if event is None:
                    return
                if event["seq"] <= last:
                    continue
                last = event["seq"]
                yield event
        finally:
            self._subscribers.discard(queue)


TurnRunner = Callable[[Turn], Awaitable[None]]


class TurnManager:
    def __init__(self, retention_seconds: float = 900.0):
        self.retention_seconds = retention_seconds
        self._latest: Dict[str, Turn] = {}
        self._by_id: Dict[str, Turn] = {}

    def start(self, thread_id: str, bot_id: str, model: str, runner: TurnRunner) -> Turn:
        self._cleanup()
        current = self._latest.get(thread_id)
        if current is not None and not current.finished:
            raise TurnBusyError(current)
        turn = Turn(
            turn_id=f"turn-{uuid.uuid4().hex[:10]}",
            thread_id=thread_id,
            bot_id=bot_id,
            model=model,
            created_at=time.time(),
        )
        self._latest[thread_id] = turn
        self._by_id[turn.turn_id] = turn
        turn.task = asyncio.get_running_loop().create_task(self._run(turn, runner))
        return turn

    async def _run(self, turn: Turn, runner: TurnRunner) -> None:
        try:
            await runner(turn)
        except asyncio.CancelledError:
            # A runner that saved its partial reply has announced the stop itself.
            if not turn.events or turn.events[-1].get("type") != "turn.cancelled":
                turn.emit({"type": "turn.cancelled", "botMsgId": None})
            turn.finish("cancelled")
            return
        except Exception as exc:  # The turn must never take the server down.
            turn.emit({"type": "turn.failed", "error": str(exc)})
            turn.finish("failed", str(exc))
            return
        turn.finish("completed")

    def current(self, thread_id: str) -> Optional[Turn]:
        return self._latest.get(thread_id)

    def get(self, turn_id: str) -> Optional[Turn]:
        return self._by_id.get(turn_id)

    def cancel(self, thread_id: str) -> Optional[Turn]:
        turn = self._latest.get(thread_id)
        if turn is None or turn.finished or turn.task is None:
            return None
        turn.task.cancel()
        return turn

    def running_for_bot(self, bot_id: str) -> List[Turn]:
        """The bot's unfinished turns, one per conversation that has one."""
        return [turn for turn in self._latest.values() if turn.bot_id == bot_id and not turn.finished]

    def cancel_bot(self, bot_id: str) -> List[Turn]:
        """Stop the bot's replies in all of its conversations."""
        return [turn for turn in (self.cancel(t.thread_id) for t in self.running_for_bot(bot_id)) if turn is not None]

    def overview(self) -> List[Dict[str, Any]]:
        """Running turns and turns waiting on an approval, for the sidebar."""
        self._cleanup()
        return [turn.to_dict() for turn in self._latest.values() if not turn.finished]

    def _cleanup(self) -> None:
        cutoff = time.time() - self.retention_seconds
        for thread_id, turn in list(self._latest.items()):
            if turn.finished and (turn.finished_at or 0) < cutoff:
                self._latest.pop(thread_id, None)
                self._by_id.pop(turn.turn_id, None)


turn_manager = TurnManager()
