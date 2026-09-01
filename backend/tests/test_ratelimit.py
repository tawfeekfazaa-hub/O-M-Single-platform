from __future__ import annotations

import pytest

from app.core.ratelimit import RateLimitExceeded, RollingWindowRateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_limiter(
    max_calls: int, window: float, clock: FakeClock, sleeps: list[float] | None = None
):
    async def fake_sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)
        clock.now += seconds

    return RollingWindowRateLimiter(max_calls, window, clock=clock, sleep=fake_sleep)


async def test_allows_up_to_max_calls_then_raises():
    clock = FakeClock()
    limiter = make_limiter(3, 600.0, clock)
    for _ in range(3):
        await limiter.acquire(wait=False)
    with pytest.raises(RateLimitExceeded) as excinfo:
        await limiter.acquire(wait=False)
    assert excinfo.value.retry_after_seconds == pytest.approx(600.0)


async def test_slot_frees_after_window_passes():
    clock = FakeClock()
    limiter = make_limiter(2, 600.0, clock)
    await limiter.acquire(wait=False)
    clock.now = 100.0
    await limiter.acquire(wait=False)
    # First slot frees at t=600.
    clock.now = 600.5
    await limiter.acquire(wait=False)
    with pytest.raises(RateLimitExceeded):
        await limiter.acquire(wait=False)


async def test_wait_mode_sleeps_exactly_until_free_slot():
    clock = FakeClock()
    sleeps: list[float] = []
    limiter = make_limiter(1, 600.0, clock, sleeps)
    await limiter.acquire()
    await limiter.acquire()  # must sleep the full window
    assert sleeps == [pytest.approx(600.0)]


async def test_retry_after_zero_when_budget_available():
    clock = FakeClock()
    limiter = make_limiter(5, 600.0, clock)
    assert limiter.retry_after() == 0.0


def test_rejects_invalid_configuration():
    with pytest.raises(ValueError):
        RollingWindowRateLimiter(0, 600.0)
    with pytest.raises(ValueError):
        RollingWindowRateLimiter(5, 0.0)
