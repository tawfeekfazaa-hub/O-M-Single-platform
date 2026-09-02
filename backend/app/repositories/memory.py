"""In-memory repository — default when DATABASE_URL is unset (dev/CI)."""

from __future__ import annotations

import itertools
from bisect import bisect_left, bisect_right
from datetime import UTC, datetime

from app.adapters.base import PlantInfo, PlantKpiReading, PlantStatus
from app.models.domain import KpiPoint, Plant
from app.repositories.base import Repository


class InMemoryRepository(Repository):
    def __init__(self) -> None:
        self._plants: dict[int, Plant] = {}
        self._by_vendor_key: dict[tuple[str, str], int] = {}
        # KPI points per plant, kept sorted by ts (scheduler appends in order).
        self._kpis: dict[int, list[KpiPoint]] = {}
        self._ids = itertools.count(1)

    async def upsert_plants(self, infos: list[PlantInfo]) -> list[Plant]:
        now = datetime.now(UTC)
        result: list[Plant] = []
        for info in infos:
            key = (info.vendor, info.vendor_plant_id)
            plant_id = self._by_vendor_key.get(key)
            if plant_id is None:
                plant_id = next(self._ids)
                self._by_vendor_key[key] = plant_id
                self._plants[plant_id] = Plant(
                    id=plant_id,
                    vendor=info.vendor,
                    vendor_plant_id=info.vendor_plant_id,
                    name=info.name,
                    capacity_kwp=info.capacity_kwp,
                    address=info.address,
                    status=PlantStatus.UNKNOWN,
                    updated_at=now,
                )
                self._kpis[plant_id] = []
            else:
                plant = self._plants[plant_id]
                plant.name = info.name
                if info.capacity_kwp is not None:
                    # Capacity is static metadata that the adapter reports
                    # as None both when the vendor omits it and when the
                    # value it sent could not be read. Overwriting a good
                    # stored value with that is silent data loss, so a
                    # missing capacity means "no update", not "unknown now".
                    plant.capacity_kwp = info.capacity_kwp
                plant.address = info.address
                plant.updated_at = now
            result.append(self._plants[plant_id])
        return result

    async def list_plants(self) -> list[Plant]:
        return sorted(self._plants.values(), key=lambda p: p.id)

    async def get_plant(self, plant_id: int) -> Plant | None:
        return self._plants.get(plant_id)

    async def record_kpis(self, readings: list[PlantKpiReading]) -> int:
        written = 0
        for reading in readings:
            plant_id = self._by_vendor_key.get((reading.vendor, reading.vendor_plant_id))
            if plant_id is None:
                continue
            points = self._kpis[plant_id]
            if points and points[-1].ts == reading.ts:
                continue  # same-timestamp duplicate, mirror the DB upsert-noop
            points.append(
                KpiPoint(
                    plant_id=plant_id,
                    ts=reading.ts,
                    active_power_kw=reading.active_power_kw,
                    daily_energy_kwh=reading.daily_energy_kwh,
                    total_energy_kwh=reading.total_energy_kwh,
                    performance_ratio=reading.performance_ratio,
                )
            )
            plant = self._plants[plant_id]
            plant.status = reading.status
            plant.updated_at = reading.ts
            written += 1
        return written

    async def latest_kpi(self, plant_id: int) -> KpiPoint | None:
        points = self._kpis.get(plant_id)
        return points[-1] if points else None

    async def kpi_history(
        self,
        plant_id: int,
        start: datetime,
        end: datetime,
        limit: int = 1000,
    ) -> list[KpiPoint]:
        points = self._kpis.get(plant_id, [])
        timestamps = [p.ts for p in points]
        lo = bisect_left(timestamps, start)
        hi = bisect_right(timestamps, end)
        # end is exclusive
        while hi > lo and points[hi - 1].ts == end:
            hi -= 1
        return points[lo:hi][:limit]
