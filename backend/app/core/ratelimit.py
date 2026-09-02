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

    def set_max_calls(self, max_calls: int) -> None:
        """Adjust the budget in place, preserving the call history.

        Used for budgets that are derived at runtime (e.g. FusionSolar's
        real-time KPI allowance scales with the number of plants).
        """
        if max_calls < 1:
            raise ValueError("max_calls must be >= 1")
        self.max_calls = max_calls

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

    def wait_for(self, count: int) -> float:
        """Seconds until ``count`` slots are free AT ONCE. 0 if they are now.

        A paginated burst needs its whole page count available before it
        starts: taking the last free slot and being rejected on the next
        page spends budget on an inventory that is never retrieved.
        """
        if count <= 0:
            return 0.0
        now = self._clock()
        self._prune(now)
        needed = count - (self.max_calls - len(self._calls))
        if needed <= 0:
            return 0.0
        if needed > len(self._calls):
            # More than the whole budget: no wait ever makes this fit. The
            # caller's page guard rejects that configuration; never return
            # an unbounded delay for a scheduler to sleep on.
            return self.window_seconds
        return self._calls[needed - 1] + self.window_seconds - now

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
