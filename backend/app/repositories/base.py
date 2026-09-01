"""Repository contract — the only data access path for API and scheduler.

Two implementations (docs/DECISIONS.md ADR-004): in-memory for dev/CI and
Postgres/TimescaleDB for staging/prod. Every query is scoped by plant_id
(per-plant isolation).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from app.adapters.base import PlantInfo, PlantKpiReading
from app.models.domain import KpiPoint, Plant


class Repository(ABC):
    @abstractmethod
    async def upsert_plants(self, infos: list[PlantInfo]) -> list[Plant]:
        """Insert new plants / refresh metadata of known ones (by vendor key)."""

    @abstractmethod
    async def list_plants(self) -> list[Plant]: ...

    @abstractmethod
    async def get_plant(self, plant_id: int) -> Plant | None: ...

    @abstractmethod
    async def record_kpis(self, readings: list[PlantKpiReading]) -> int:
        """Store KPI readings and update plant status. Returns rows written.

        Readings for unknown (vendor, vendor_plant_id) pairs are skipped —
        plants must be upserted first by the scheduler cycle.
        """

    @abstractmethod
    async def latest_kpi(self, plant_id: int) -> KpiPoint | None: ...

    @abstractmethod
    async def kpi_history(
        self,
        plant_id: int,
        start: datetime,
        end: datetime,
        limit: int = 1000,
    ) -> list[KpiPoint]:
        """Readings in [start, end), ascending by ts, capped at ``limit``."""

    async def close(self) -> None:
        return None
