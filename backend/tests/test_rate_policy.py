"""Per-endpoint FusionSolar rate policy tests (all offline, fake clock)."""

from __future__ import annotations

import pytest

from app.adapters.base import AdapterRateLimitError
from app.adapters.fusionsolar.policy import Endpoint, FusionSolarRatePolicy
from app.config import Settings


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def _fail_sleep(_seconds: float) -> None:  # policy must never sleep
    raise AssertionError("rate policy must not sleep")


def make_policy(clock: FakeClock) -> FusionSolarRatePolicy:
    return FusionSolarRatePolicy(clock=clock, sleep=_fail_sleep)


async def test_budgets_are_independent_per_endpoint():
    clock = FakeClock()
    policy = make_policy(clock)
    for _ in range(4):
        await policy.acquire(Endpoint.LOGIN)
    with pytest.raises(AdapterRateLimitError):
        await policy.acquire(Endpoint.LOGIN)
    # Exhausting login must not consume station-list or KPI budgets.
    await policy.acquire(Endpoint.STATION_LIST)
    await policy.acquire(Endpoint.STATION_REAL_KPI)


async def test_login_budget_keeps_margin_below_official_five():
    clock = FakeClock()
    policy = make_policy(clock)
    for _ in range(4):
        await policy.acquire(Endpoint.LOGIN)
    with pytest.raises(AdapterRateLimitError) as excinfo:
        await policy.acquire(Endpoint.LOGIN)
    assert excinfo.value.retry_after_seconds == pytest.approx(600.0)


async def test_station_list_daily_safety_budget():
    clock = FakeClock()
    policy = make_policy(clock)
    for _ in range(4):
        await policy.acquire(Endpoint.STATION_LIST)
    with pytest.raises(AdapterRateLimitError):
        await policy.acquire(Endpoint.STATION_LIST)
    clock.now = 86_400.5  # slot frees after the daily window
    await policy.acquire(Endpoint.STATION_LIST)


@pytest.mark.parametrize(
    ("plants", "calls"),
    [(0, 1), (1, 1), (99, 1), (100, 1), (101, 2), (200, 2), (250, 3)],
)
def test_official_kpi_allowance_formula(plants: int, calls: int):
    assert FusionSolarRatePolicy.kpi_calls_for(plants) == calls


async def test_kpi_budget_scales_with_plant_count():
    clock = FakeClock()
    policy = make_policy(clock)
    policy.set_kpi_plant_count(250)  # ceil(250/100) = 3 calls per window
    for _ in range(3):
        await policy.acquire(Endpoint.STATION_REAL_KPI)
    with pytest.raises(AdapterRateLimitError):
        await policy.acquire(Endpoint.STATION_REAL_KPI)
    clock.now = 300.5
    await policy.acquire(Endpoint.STATION_REAL_KPI)


async def test_kpi_budget_resize_preserves_call_history():
    clock = FakeClock()
    policy = make_policy(clock)
    policy.set_kpi_plant_count(200)
    await policy.acquire(Endpoint.STATION_REAL_KPI)
    await policy.acquire(Endpoint.STATION_REAL_KPI)
    policy.set_kpi_plant_count(100)  # shrink to 1/window; 2 already spent
    with pytest.raises(AdapterRateLimitError):
        await policy.acquire(Endpoint.STATION_REAL_KPI)


def test_retry_after_hint_is_at_least_the_endpoint_window():
    clock = FakeClock()
    policy = make_policy(clock)
    assert policy.retry_after_hint(Endpoint.STATION_REAL_KPI) >= 300.0
    assert policy.retry_after_hint(Endpoint.LOGIN) >= 600.0
    assert policy.retry_after_hint(Endpoint.STATION_LIST) >= 86_400.0


def test_from_settings_uses_configured_budgets():
    settings = Settings(
        _env_file=None,
        fusionsolar_login_max_calls=2,
        fusionsolar_station_list_window_seconds=3600.0,
    )
    policy = FusionSolarRatePolicy.from_settings(settings)
    assert policy.window_seconds(Endpoint.STATION_LIST) == 3600.0
