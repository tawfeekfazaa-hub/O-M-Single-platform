"""Per-endpoint FusionSolar rate policy.

Replaces the pre-PR-1 single shared budget, which did not model Huawei's
documented per-endpoint limits (docs/FUSIONSOLAR-CONTRACT.md):

- login:            5 calls / 10 min per user (OFFICIAL) — default keeps a
                    margin of one call.
- getStationRealKpi ceil(plants/100) calls / 5 min, max 100 station codes
                    per call (OFFICIAL). The allowance is derived at
                    runtime from the requested plant count.
- getStationList    a small daily-style budget whose exact formula varies
                    by SmartPVMS version; 4/day here is a SAFETY DEFAULT,
                    not an official constant.

All vendor calls are sequential — a budget never authorizes concurrency —
and exhausting one budget never triggers a retry that spends another.
"""

from __future__ import annotations

import asyncio
import enum
import math
import time
from collections.abc import Awaitable, Callable

from app.adapters.base import AdapterRateLimitError
from app.config import Settings
from app.core.ratelimit import RateLimitExceeded, RollingWindowRateLimiter

KPI_BATCH_SIZE = 100  # official maximum station codes per getStationRealKpi call


class Endpoint(enum.StrEnum):
    LOGIN = "login"
    STATION_LIST = "station_list"
    STATION_REAL_KPI = "station_real_kpi"


class FusionSolarRatePolicy:
    """Owns one rolling-window limiter per endpoint."""

    def __init__(
        self,
        *,
        login_max_calls: int = 4,
        login_window_seconds: float = 600.0,
        station_list_max_calls: int = 4,
        station_list_window_seconds: float = 86_400.0,
        kpi_window_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._limiters: dict[Endpoint, RollingWindowRateLimiter] = {
            Endpoint.LOGIN: RollingWindowRateLimiter(
                login_max_calls, login_window_seconds, clock=clock, sleep=sleep
            ),
            Endpoint.STATION_LIST: RollingWindowRateLimiter(
                station_list_max_calls, station_list_window_seconds, clock=clock, sleep=sleep
            ),
            # Starts at the documented minimum (1 call/window) and is
            # re-derived from the plant count via set_kpi_plant_count().
            Endpoint.STATION_REAL_KPI: RollingWindowRateLimiter(
                1, kpi_window_seconds, clock=clock, sleep=sleep
            ),
        }

    @classmethod
    def from_settings(cls, settings: Settings) -> FusionSolarRatePolicy:
        return cls(
            login_max_calls=settings.fusionsolar_login_max_calls,
            login_window_seconds=settings.fusionsolar_login_window_seconds,
            station_list_max_calls=settings.fusionsolar_station_list_max_calls,
            station_list_window_seconds=settings.fusionsolar_station_list_window_seconds,
            kpi_window_seconds=settings.fusionsolar_kpi_window_seconds,
        )

    @staticmethod
    def kpi_calls_for(plant_count: int) -> int:
        """Official allowance: ceil(plants/100), never below one call."""
        return max(1, math.ceil(max(plant_count, 0) / KPI_BATCH_SIZE))

    def set_kpi_plant_count(self, plant_count: int) -> None:
        self._limiters[Endpoint.STATION_REAL_KPI].set_max_calls(self.kpi_calls_for(plant_count))

    def window_seconds(self, endpoint: Endpoint) -> float:
        return self._limiters[endpoint].window_seconds

    def retry_after_hint(self, endpoint: Endpoint) -> float:
        """Lower-bound wait for a vendor-side rejection on this endpoint."""
        limiter = self._limiters[endpoint]
        return max(limiter.retry_after(), limiter.window_seconds)

    async def acquire(self, endpoint: Endpoint) -> None:
        """Reserve one slot or raise AdapterRateLimitError (never waits)."""
        try:
            await self._limiters[endpoint].acquire(wait=False)
        except RateLimitExceeded as exc:
            raise AdapterRateLimitError(
                f"client-side FusionSolar {endpoint.value} budget exhausted",
                retry_after_seconds=exc.retry_after_seconds,
            ) from exc
