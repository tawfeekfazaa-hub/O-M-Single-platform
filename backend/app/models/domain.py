"""Internal domain model — what the repository stores and the API serves."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.adapters.base import PlantStatus


@dataclass(slots=True)
class Plant:
    id: int
    vendor: str
    vendor_plant_id: str
    name: str
    capacity_kwp: float | None
    address: str | None
    status: PlantStatus
    updated_at: datetime


@dataclass(slots=True)
class KpiPoint:
    plant_id: int
    ts: datetime
    active_power_kw: float | None
    daily_energy_kwh: float | None
    total_energy_kwh: float | None
    performance_ratio: float | None
