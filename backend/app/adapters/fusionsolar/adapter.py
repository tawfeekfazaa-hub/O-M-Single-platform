"""FusionSolar adapter: maps vendor payloads to the normalized model.

The mapping treats every vendor field as optional — field availability
differs by tenant/version (docs/API-NOTES.md).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from app.adapters.base import PlantInfo, PlantKpiReading, PlantStatus, VendorAdapter
from app.adapters.fusionsolar.client import FusionSolarClient

_HEALTH_TO_STATUS = {
    1: PlantStatus.DISCONNECTED,
    2: PlantStatus.FAULTY,
    3: PlantStatus.HEALTHY,
}

# getStationRealKpi accepts up to 100 station codes per call.
KPI_BATCH_SIZE = 100


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class FusionSolarAdapter(VendorAdapter):
    vendor: ClassVar[str] = "fusionsolar"

    def __init__(self, client: FusionSolarClient) -> None:
        self._client = client

    async def authenticate(self) -> None:
        if not self._client.is_logged_in():
            await self._client.login()

    async def list_plants(self) -> list[PlantInfo]:
        stations = await self._client.get_station_list()
        plants: list[PlantInfo] = []
        for station in stations:
            code = station.get("stationCode")
            if not code:
                continue
            capacity_mw = _as_float(station.get("capacity"))
            plants.append(
                PlantInfo(
                    vendor=self.vendor,
                    vendor_plant_id=str(code),
                    name=str(station.get("stationName") or code),
                    # FusionSolar reports capacity in MW; we store kWp.
                    capacity_kwp=capacity_mw * 1000.0 if capacity_mw is not None else None,
                    address=station.get("stationAddr"),
                )
            )
        return plants

    async def fetch_plant_kpis(self, vendor_plant_ids: list[str]) -> list[PlantKpiReading]:
        readings: list[PlantKpiReading] = []
        for start in range(0, len(vendor_plant_ids), KPI_BATCH_SIZE):
            batch = vendor_plant_ids[start : start + KPI_BATCH_SIZE]
            rows = await self._client.get_station_real_kpi(batch)
            ts = datetime.now(UTC)
            for row in rows:
                code = row.get("stationCode")
                if not code:
                    continue
                item = row.get("dataItemMap") or {}
                health = _as_int(item.get("real_health_state"))
                readings.append(
                    PlantKpiReading(
                        vendor=self.vendor,
                        vendor_plant_id=str(code),
                        ts=ts,
                        active_power_kw=_as_float(item.get("real_power")),
                        daily_energy_kwh=_as_float(item.get("day_power")),
                        total_energy_kwh=_as_float(item.get("total_power")),
                        performance_ratio=_as_float(item.get("performance_ratio")),
                        status=_HEALTH_TO_STATUS.get(health, PlantStatus.UNKNOWN),
                    )
                )
        return readings

    async def health_check(self) -> bool:
        # Must not consume vendor rate budget: session presence only.
        return self._client.is_logged_in()

    async def close(self) -> None:
        await self._client.close()
