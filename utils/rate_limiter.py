"""Rate limiting and bounded retries.

- `AsyncRateLimiter`: minimum interval between calls to a shared resource.
- `retry_async`: bounded exponential backoff with jitter; never infinite.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Awaitable, Callable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class AsyncRateLimiter:
    """Enforces a minimum gap between acquisitions (token-bucket of size 1)."""

    def __init__(self, min_interval_s: float = 0.0):
        self.min_interval = min_interval_s
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self.min_interval - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def __aenter__(self) -> "AsyncRateLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 3,
    base_delay_s: float = 1.0,
    max_delay_s: float = 30.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    description: str = "operation",
) -> T:
    """Run `fn` with bounded exponential backoff. Raises the last error when exhausted."""
    last_error: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except retry_on as exc:  # noqa: BLE001 — intentionally broad, bounded
            last_error = exc
            if attempt >= max_retries:
                break
            delay = min(max_delay_s, base_delay_s * (2**attempt)) * (0.8 + 0.4 * random.random())
            log.warning(
                "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                description, attempt + 1, max_retries + 1, exc, delay,
            )
            await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error
