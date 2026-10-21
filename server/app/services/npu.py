"""One way to call the model server's small NPU models, with retries.

The decider, the embedding model and the reranker run on halogen's NPU,
which has been crashing now and then; halogen restarts it within a few
seconds, but a request in flight hangs or fails. Every call to those models
goes through here: a short per-attempt timeout so a hung request is cut
off, and a retry with a pause long enough for the restart. Transport
errors, timeouts and 5xx/429/408 replies are retried; anything else is
returned as it came.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
# One NPU request at a time from this server. halogen runs them one after
# another anyway, and two large requests in flight together is what filled
# the device heap and hung it (2026-10-21). The lock belongs to the running
# event loop, so it is made for the loop that first needs it (tests run many).
_lock: Optional[asyncio.Lock] = None
_lock_loop: Optional[asyncio.AbstractEventLoop] = None


def npu_lock() -> asyncio.Lock:
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    return _lock


def _backoff() -> list:
    try:
        values = [float(x) for x in str(settings.NPU_RETRY_BACKOFF_SECONDS).split(",") if x.strip()]
    except ValueError:
        values = []
    return values or [1.0, 3.0]


def client_factory(timeout: httpx.Timeout, headers: Optional[Dict[str, str]] = None) -> httpx.AsyncClient:
    """Tests replace this to hand in a mock transport."""
    return httpx.AsyncClient(timeout=timeout, headers=headers or {})


class NpuError(RuntimeError):
    """The call failed after every attempt."""


async def post_json_with_retry(
    url: str,
    payload: Dict[str, Any],
    *,
    timeout: httpx.Timeout,
    headers: Optional[Dict[str, str]] = None,
    label: str = "NPU",
    attempts: Optional[int] = None,
) -> httpx.Response:
    """POST `payload` as JSON, retrying a failed or hung attempt. Returns the final response.

    Raises NpuError when no attempt produced a response worth returning.
    """
    tries = max(1, int(settings.NPU_RETRY_ATTEMPTS if attempts is None else attempts))
    pauses = _backoff()
    last_error: Optional[str] = None
    async with npu_lock():
        for attempt in range(1, tries + 1):
            try:
                async with client_factory(timeout, headers) as client:
                    response = await client.post(url, json=payload)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}"
                response = None
            if response is not None and response.status_code not in RETRY_STATUSES:
                return response
            if response is not None:
                last_error = f"HTTP {response.status_code}"
            if attempt < tries:
                pause = pauses[min(attempt - 1, len(pauses) - 1)]
                logger.warning("%s call failed (%s), attempt %d of %d; retrying in %.0fs", label, last_error, attempt, tries, pause)
                await asyncio.sleep(pause)
    raise NpuError(f"{label} unavailable after {tries} attempts ({last_error})")
