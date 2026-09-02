"""Deterministic FusionSolar mock client (legacy_system_code profile).

Produces *vendor-shaped* payloads so the adapter's mapping runs the same
code path in mock and real mode. All randomness is CRC32-derived — same
inputs, same outputs, on every machine (CLAUDE.md rule 3).

SYNTHETIC MOCK-ONLY DATA — read before comparing with the real API:
the KPI rows include ``real_power`` (station active power) and
``performance_ratio``, which are NOT part of the officially documented
``getStationRealKpi`` dataItemMap. They exist here purely so the MVP
dashboard and the interface tests have plausible values to render. The
real adapter never derives active power from an undocumented field
(docs/FUSIONSOLAR-CONTRACT.md).

Both documented station-list response variants can be emulated via
``station_list_variant`` ("direct_list" — default, or "paginated"), and a
small ``page_size`` lets tests exercise multi-page retrieval offline.
"""

from __future__ import annotations

import math
import zlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from app.adapters.fusionsolar.client import (
    ClientCallCounts,
    KpiBatchResult,
    StationListResult,
)

# Riyadh is UTC+3, no DST.
LOCAL_UTC_OFFSET = timedelta(hours=3)
SUNRISE_HOUR = 6.0
SUNSET_HOUR = 18.0
# Typical specific yield for a good Saudi site, kWh/kWp/day. (synthetic)
DAILY_YIELD_KWH_PER_KWP = 5.5

HEALTH_DISCONNECTED = 1
HEALTH_FAULTY = 2
HEALTH_HEALTHY = 3

MOCK_STATIONS: list[dict[str, Any]] = [
    # FusionSolar reports "capacity" in MW.
    {
        "stationCode": "NE=MOCK001",
        "stationName": "AQ Riyadh Solar Park 1",
        "capacity": 2.5,
        "stationAddr": "Riyadh, Saudi Arabia",
        "_health": HEALTH_HEALTHY,
    },
    {
        "stationCode": "NE=MOCK002",
        "stationName": "AQ Jeddah Rooftop Cluster",
        "capacity": 0.85,
        "stationAddr": "Jeddah, Saudi Arabia",
        "_health": HEALTH_HEALTHY,
    },
    {
        "stationCode": "NE=MOCK003",
        "stationName": "AQ Dammam Industrial PV",
        "capacity": 1.2,
        "stationAddr": "Dammam, Saudi Arabia",
        "_health": HEALTH_FAULTY,
    },
]


def _noise(key: str, low: float, high: float) -> float:
    """Deterministic pseudo-noise in [low, high] derived from ``key``."""
    fraction = (zlib.crc32(key.encode()) % 10_000) / 10_000
    return low + (high - low) * fraction


def _daylight_fraction(local_dt: datetime) -> float:
    """0 at sunrise, 1 at sunset, clamped outside daylight."""
    hour = local_dt.hour + local_dt.minute / 60 + local_dt.second / 3600
    span = SUNSET_HOUR - SUNRISE_HOUR
    return min(1.0, max(0.0, (hour - SUNRISE_HOUR) / span))


class MockFusionSolarClient:
    """Same call surface as ``RealFusionSolarClient``, zero network."""

    def __init__(
        self,
        *,
        now: Callable[[], datetime] | None = None,
        station_list_variant: Literal["direct_list", "paginated"] = "direct_list",
        page_size: int = 100,
        stations: list[dict[str, Any]] | None = None,
    ) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._variant = station_list_variant
        self._page_size = page_size
        self._stations = stations if stations is not None else MOCK_STATIONS
        self._counts = ClientCallCounts()

    def is_logged_in(self) -> bool:
        return True

    def call_counts(self) -> ClientCallCounts:
        return self._counts

    async def login(self) -> None:
        self._counts.login += 1

    def _public_rows(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in s.items() if not k.startswith("_")} for s in self._stations]

    async def list_stations(self) -> StationListResult:
        rows = self._public_rows()
        if self._variant == "direct_list":
            self._counts.station_list += 1
            return StationListResult(stations=rows, variant="direct_list", pages_retrieved=1)
        pages = [rows[i : i + self._page_size] for i in range(0, len(rows), self._page_size)] or [
            []
        ]
        self._counts.station_list += len(pages)
        return StationListResult(stations=rows, variant="paginated", pages_retrieved=len(pages))

    async def get_station_real_kpi(self, station_codes: list[str]) -> KpiBatchResult:
        self._counts.station_real_kpi += 1
        now_utc = self._now()
        local = now_utc.astimezone(UTC) + LOCAL_UTC_OFFSET
        rows: list[dict[str, Any]] = []
        for station in self._stations:
            code = str(station["stationCode"])
            if code not in station_codes:
                continue
            capacity_kw = float(station["capacity"]) * 1000.0
            x = _daylight_fraction(local)
            bucket = local.strftime("%Y%m%d%H%M")
            wobble = _noise(f"{code}:{bucket}", 0.95, 1.05)

            # Bell-shaped production curve; fully derated when not healthy.
            derate = 1.0 if station["_health"] == HEALTH_HEALTHY else 0.3
            power_kw = capacity_kw * 0.85 * math.sin(math.pi * x) * wobble * derate
            # Fraction of the day's energy produced so far = ∫sin over [0,x].
            energy_fraction = (1.0 - math.cos(math.pi * x)) / 2.0
            day_energy_kwh = capacity_kw * DAILY_YIELD_KWH_PER_KWP * energy_fraction * derate
            lifetime_base_kwh = _noise(f"{code}:lifetime", 1.0e6, 8.0e6)

            rows.append(
                {
                    "stationCode": code,
                    "dataItemMap": {
                        # SYNTHETIC mock-only fields (not in the documented
                        # getStationRealKpi contract): real_power,
                        # performance_ratio. Kept for the MVP dashboard.
                        "real_power": round(max(power_kw, 0.0), 3),
                        "performance_ratio": round(_noise(f"{code}:pr:{bucket}", 0.75, 0.85), 4),
                        # Documented contract fields:
                        "day_power": round(day_energy_kwh, 3),
                        "total_power": round(lifetime_base_kwh + day_energy_kwh, 3),
                        "real_health_state": station["_health"],
                    },
                }
            )
        return KpiBatchResult(
            rows=rows,
            vendor_current_time_ms=int(now_utc.timestamp() * 1000),
        )

    async def close(self) -> None:
        return None
