"""Deterministic FusionSolar mock client.

Produces *vendor-shaped* payloads (same structure as the real Northbound
API) so the adapter's mapping code runs identically in mock and real mode.
All randomness is derived from CRC32 hashes — same inputs, same outputs,
on every machine and every run (CLAUDE.md rule 3: develop against mocks).
"""

from __future__ import annotations

import math
import zlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

# Riyadh is UTC+3, no DST.
LOCAL_UTC_OFFSET = timedelta(hours=3)
SUNRISE_HOUR = 6.0
SUNSET_HOUR = 18.0
# Typical specific yield for a good Saudi site, kWh/kWp/day.
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

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self.call_count = 0

    def is_logged_in(self) -> bool:
        return True

    async def login(self) -> None:
        self.call_count += 1

    async def get_station_list(self) -> list[dict[str, Any]]:
        self.call_count += 1
        return [{k: v for k, v in s.items() if not k.startswith("_")} for s in MOCK_STATIONS]

    async def get_station_real_kpi(self, station_codes: list[str]) -> list[dict[str, Any]]:
        self.call_count += 1
        now_utc = self._now()
        local = now_utc.astimezone(UTC) + LOCAL_UTC_OFFSET
        results: list[dict[str, Any]] = []
        for station in MOCK_STATIONS:
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

            results.append(
                {
                    "stationCode": code,
                    "dataItemMap": {
                        "real_power": round(max(power_kw, 0.0), 3),
                        "day_power": round(day_energy_kwh, 3),
                        "total_power": round(lifetime_base_kwh + day_energy_kwh, 3),
                        "performance_ratio": round(_noise(f"{code}:pr:{bucket}", 0.75, 0.85), 4),
                        "real_health_state": station["_health"],
                    },
                }
            )
        return results

    async def close(self) -> None:
        return None
