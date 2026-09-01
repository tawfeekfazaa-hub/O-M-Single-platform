"""Postgres/TimescaleDB repository — staging/prod persistence."""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.adapters.base import PlantInfo, PlantKpiReading, PlantStatus
from app.db.tables import kpi_measurements, plants
from app.models.domain import KpiPoint, Plant
from app.repositories.base import Repository


def _row_to_plant(row: sa.Row) -> Plant:
    return Plant(
        id=row.id,
        vendor=row.vendor,
        vendor_plant_id=row.vendor_plant_id,
        name=row.name,
        capacity_kwp=row.capacity_kwp,
        address=row.address,
        status=PlantStatus(row.status),
        updated_at=row.updated_at,
    )


def _row_to_kpi(row: sa.Row) -> KpiPoint:
    return KpiPoint(
        plant_id=row.plant_id,
        ts=row.ts,
        active_power_kw=row.active_power_kw,
        daily_energy_kwh=row.daily_energy_kwh,
        total_energy_kwh=row.total_energy_kwh,
        performance_ratio=row.performance_ratio,
    )


class PostgresRepository(Repository):
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, database_url: str) -> PostgresRepository:
        return cls(create_async_engine(database_url, pool_pre_ping=True))

    async def upsert_plants(self, infos: list[PlantInfo]) -> list[Plant]:
        if not infos:
            return []
        now = datetime.now(UTC)
        values = [
            {
                "vendor": i.vendor,
                "vendor_plant_id": i.vendor_plant_id,
                "name": i.name,
                "capacity_kwp": i.capacity_kwp,
                "address": i.address,
                "created_at": now,
                "updated_at": now,
            }
            for i in infos
        ]
        stmt = pg_insert(plants).values(values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_plants_vendor_key",
            set_={
                "name": stmt.excluded.name,
                "capacity_kwp": stmt.excluded.capacity_kwp,
                "address": stmt.excluded.address,
                "updated_at": stmt.excluded.updated_at,
            },
        ).returning(plants)
        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).fetchall()
        return [_row_to_plant(r) for r in rows]

    async def list_plants(self) -> list[Plant]:
        async with self._engine.connect() as conn:
            rows = (await conn.execute(sa.select(plants).order_by(plants.c.id))).fetchall()
        return [_row_to_plant(r) for r in rows]

    async def get_plant(self, plant_id: int) -> Plant | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(sa.select(plants).where(plants.c.id == plant_id))
            ).one_or_none()
        return _row_to_plant(row) if row else None

    async def record_kpis(self, readings: list[PlantKpiReading]) -> int:
        if not readings:
            return 0
        written = 0
        async with self._engine.begin() as conn:
            key_rows = (
                await conn.execute(
                    sa.select(plants.c.id, plants.c.vendor, plants.c.vendor_plant_id)
                )
            ).fetchall()
            ids = {(r.vendor, r.vendor_plant_id): r.id for r in key_rows}
            for reading in readings:
                plant_id = ids.get((reading.vendor, reading.vendor_plant_id))
                if plant_id is None:
                    continue
                stmt = (
                    pg_insert(kpi_measurements)
                    .values(
                        ts=reading.ts,
                        plant_id=plant_id,
                        active_power_kw=reading.active_power_kw,
                        daily_energy_kwh=reading.daily_energy_kwh,
                        total_energy_kwh=reading.total_energy_kwh,
                        performance_ratio=reading.performance_ratio,
                    )
                    .on_conflict_do_nothing(index_elements=["plant_id", "ts"])
                )
                result = await conn.execute(stmt)
                if result.rowcount:
                    written += result.rowcount
                    await conn.execute(
                        sa.update(plants)
                        .where(plants.c.id == plant_id)
                        .values(status=reading.status.value, updated_at=reading.ts)
                    )
        return written

    async def latest_kpi(self, plant_id: int) -> KpiPoint | None:
        stmt = (
            sa.select(kpi_measurements)
            .where(kpi_measurements.c.plant_id == plant_id)
            .order_by(kpi_measurements.c.ts.desc())
            .limit(1)
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(stmt)).one_or_none()
        return _row_to_kpi(row) if row else None

    async def kpi_history(
        self,
        plant_id: int,
        start: datetime,
        end: datetime,
        limit: int = 1000,
    ) -> list[KpiPoint]:
        stmt = (
            sa.select(kpi_measurements)
            .where(
                kpi_measurements.c.plant_id == plant_id,
                kpi_measurements.c.ts >= start,
                kpi_measurements.c.ts < end,
            )
            .order_by(kpi_measurements.c.ts.asc())
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).fetchall()
        return [_row_to_kpi(r) for r in rows]

    async def close(self) -> None:
        await self._engine.dispose()
