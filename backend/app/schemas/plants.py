"""API response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.adapters.base import PlantStatus
from app.models.domain import KpiPoint, Plant


class PlantOut(BaseModel):
    id: int
    vendor: str
    vendor_plant_id: str
    name: str
    capacity_kwp: float | None
    address: str | None
    status: PlantStatus
    updated_at: datetime

    @classmethod
    def from_domain(cls, plant: Plant) -> PlantOut:
        return cls(
            id=plant.id,
            vendor=plant.vendor,
            vendor_plant_id=plant.vendor_plant_id,
            name=plant.name,
            capacity_kwp=plant.capacity_kwp,
            address=plant.address,
            status=plant.status,
            updated_at=plant.updated_at,
        )


class KpiPointOut(BaseModel):
    ts: datetime
    active_power_kw: float | None
    daily_energy_kwh: float | None
    total_energy_kwh: float | None
    performance_ratio: float | None

    @classmethod
    def from_domain(cls, point: KpiPoint) -> KpiPointOut:
        return cls(
            ts=point.ts,
            active_power_kw=point.active_power_kw,
            daily_energy_kwh=point.daily_energy_kwh,
            total_energy_kwh=point.total_energy_kwh,
            performance_ratio=point.performance_ratio,
        )


class PlantWithLatestKpiOut(PlantOut):
    latest_kpi: KpiPointOut | None = None


class HealthOut(BaseModel):
    status: str
    fusionsolar_mode: str
    scheduler_enabled: bool
    scheduler_cycles_total: int
    scheduler_cycles_failed: int
