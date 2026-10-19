"""A gate in front of the model server: at most N requests at a time, fairly shared.

Every chat turn, tool round and compaction summary is one streaming request
to the model server. Without a gate they all go at once, and a server like
halogen-flash-server that runs four in parallel ends up queueing the rest
silently, or refusing them. This gate keeps the number of in-flight requests
at the configured limit and makes the wait visible: a request that has to
wait yields `model.queued` events (how many are ahead of it) until it gets a
slot, then `model.started`.

Slots are handed out round-robin between accounts, oldest request first
within an account, so one person with ten busy bots cannot crowd out a
co-worker with one. The limit is the app setting `llm_max_concurrent`
(default from LLM_MAX_CONCURRENT_REQUESTS); 0 means no limit. A change to
the setting applies to the next request: requests already running finish.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, AsyncIterator, Callable, Deque, Dict, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

SETTING_KEY = "llm_max_concurrent"
MAX_LIMIT = 64


def _default_limit() -> int:
    from app.services import storage_service as storage_module

    try:
        value = storage_module.storage_service.get_app_settings().get(SETTING_KEY)
    except Exception:  # settings unavailable (tests, startup): fall back to the environment
        value = None
    if value is None or value == "":
        value = settings.LLM_MAX_CONCURRENT_REQUESTS
    try:
        return max(0, min(MAX_LIMIT, int(value)))
    except (TypeError, ValueError):
        return max(0, min(MAX_LIMIT, settings.LLM_MAX_CONCURRENT_REQUESTS))


@dataclass
class _Waiter:
    owner: str
    joined_at: float
    # ("ahead", n) while queued; ("go", None) when a slot is granted.
    inbox: "asyncio.Queue[tuple]" = field(default_factory=asyncio.Queue)
    granted: bool = False


class ModelSlots:
    def __init__(self, limit: Optional[Callable[[], int]] = None):
        self._limit = limit or _default_limit
        self._active: Dict[str, int] = {}          # owner -> running requests
        self._queues: Dict[str, Deque[_Waiter]] = {}
        self._rotation: List[str] = []             # owners in the order they are next served
        self._active_total = 0

    # ---- what is going on --------------------------------------------------------

    def limit(self) -> int:
        return self._limit()

    def snapshot(self) -> Dict[str, Any]:
        waiting = {owner: len(queue) for owner, queue in self._queues.items() if queue}
        return {
            "limit": self.limit(),
            "active": self._active_total,
            "waiting": sum(waiting.values()),
            "active_by_owner": {owner: n for owner, n in self._active.items() if n},
            "waiting_by_owner": waiting,
        }

    # ---- the queue ---------------------------------------------------------------

    def _ahead_of(self, waiter: _Waiter) -> int:
        """How many queued requests are served before this one, in round-robin order."""
        queues = {owner: list(queue) for owner, queue in self._queues.items() if queue}
        order = [owner for owner in self._rotation if owner in queues]
        ahead = 0
        while order:
            for owner in list(order):
                head = queues[owner].pop(0)
                if head is waiter:
                    return ahead
                ahead += 1
                if not queues[owner]:
                    order.remove(owner)
        return ahead

    def _dispatch(self) -> None:
        """Grant slots while there is room, one owner after another."""
        limit = self.limit()
        while any(self._queues.values()) and (limit <= 0 or self._active_total < limit):
            for owner in list(self._rotation):
                queue = self._queues.get(owner)
                if not queue:
                    continue
                waiter = queue.popleft()
                # The owner goes to the back of the rotation after being served.
                self._rotation.remove(owner)
                self._rotation.append(owner)
                self._grant(waiter)
                break
            limit = self.limit()
        self._notify_positions()

    def _notify_positions(self, except_for: Optional[_Waiter] = None) -> None:
        """Everyone still queued learns their place: it changes on every grant, leave and join."""
        for queue in self._queues.values():
            for waiter in queue:
                if waiter is not except_for:
                    waiter.inbox.put_nowait(("ahead", self._ahead_of(waiter)))

    def _grant(self, waiter: _Waiter) -> None:
        waiter.granted = True
        self._active[waiter.owner] = self._active.get(waiter.owner, 0) + 1
        self._active_total += 1
        waiter.inbox.put_nowait(("go", None))

    def _release(self, owner: str) -> None:
        self._active[owner] = max(0, self._active.get(owner, 0) - 1)
        self._active_total = max(0, self._active_total - 1)
        self._dispatch()

    def _join(self, owner: str) -> _Waiter:
        waiter = _Waiter(owner=owner, joined_at=time.monotonic())
        if owner not in self._rotation:
            self._rotation.append(owner)
        self._queues.setdefault(owner, deque()).append(waiter)
        # A new account in the rotation moves ahead of the others' later requests.
        self._notify_positions(except_for=waiter)
        return waiter

    def _leave(self, waiter: _Waiter) -> None:
        queue = self._queues.get(waiter.owner)
        if queue and waiter in queue:
            queue.remove(waiter)
            self._dispatch()

    async def gated(self, owner: str, stream: AsyncIterator[Dict[str, Any]]) -> AsyncGenerator[Dict[str, Any], None]:
        """Run `stream` inside a slot, yielding queue events first if it has to wait."""
        owner = owner or "unknown"
        limit = self.limit()
        waiter: Optional[_Waiter] = None
        acquired = False
        try:
            if limit > 0 and (self._active_total >= limit or any(self._queues.values())):
                waiter = self._join(owner)
                yield {"type": "model.queued", "ahead": self._ahead_of(waiter), "limit": limit}
                while True:
                    kind, value = await waiter.inbox.get()
                    if kind == "go":
                        acquired = True
                        break
                    yield {"type": "model.queued", "ahead": value, "limit": self.limit()}
                yield {"type": "model.started", "waited_ms": int((time.monotonic() - waiter.joined_at) * 1000)}
            else:
                self._active[owner] = self._active.get(owner, 0) + 1
                self._active_total += 1
                acquired = True
            async for event in stream:
                yield event
        finally:
            # A slot granted while we were being cancelled still counts as held.
            if acquired or (waiter is not None and waiter.granted):
                self._release(owner)
            elif waiter is not None:
                self._leave(waiter)
            close = getattr(stream, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass


model_slots = ModelSlots()
