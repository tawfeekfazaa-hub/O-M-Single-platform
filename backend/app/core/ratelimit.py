"""Client-side rolling-window rate limiter.

Protects vendor APIs with hard call budgets (FusionSolar: ~5 calls / 10 min
per user, failCode 407 on excess). Clock and sleep are injectable so tests
run instantly.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable


class RateLimitExceeded(Exception):
    def __init__(self, retry_after_seconds: float) -> None:
        super().__init__(f"rate limit exceeded, retry after {retry_after_seconds:.1f}s")
        self.retry_after_seconds = retry_after_seconds


class RollingWindowRateLimiter:
    """Allows at most ``max_calls`` per rolling ``window_seconds``."""

    def __init__(
        self,
        max_calls: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._calls and self._calls[0] <= cutoff:
            self._calls.popleft()

    def retry_after(self) -> float:
        """Seconds until the next slot frees up. 0 if a call is allowed now."""
        now = self._clock()
        self._prune(now)
        if len(self._calls) < self.max_calls:
            return 0.0
        return self._calls[0] + self.window_seconds - now

    async def acquire(self, *, wait: bool = True) -> None:
        """Reserve one call slot.

        With ``wait=True`` sleeps until a slot is available; with
        ``wait=False`` raises :class:`RateLimitExceeded` instead.
        """
        while True:
            delay = self.retry_after()
            if delay <= 0:
                self._calls.append(self._clock())
                return
            if not wait:
                raise RateLimitExceeded(delay)
            await self._sleep(delay)
