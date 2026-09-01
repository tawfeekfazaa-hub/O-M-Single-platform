from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_repository
from app.models.domain import Plant
from app.repositories.base import Repository
from app.schemas.plants import KpiPointOut, PlantWithLatestKpiOut

router = APIRouter(prefix="/plants", tags=["plants"])

RepositoryDep = Annotated[Repository, Depends(get_repository)]


async def _get_plant_or_404(repository: Repository, plant_id: int) -> Plant:
    plant = await repository.get_plant(plant_id)
    if plant is None:
        raise HTTPException(status_code=404, detail=f"plant {plant_id} not found")
    return plant


async def _with_latest(repository: Repository, plant: Plant) -> PlantWithLatestKpiOut:
    latest = await repository.latest_kpi(plant.id)
    out = PlantWithLatestKpiOut.from_domain(plant)
    if latest is not None:
        out.latest_kpi = KpiPointOut.from_domain(latest)
    return out


@router.get("", response_model=list[PlantWithLatestKpiOut])
async def list_plants(repository: RepositoryDep) -> list[PlantWithLatestKpiOut]:
    plants = await repository.list_plants()
    return [await _with_latest(repository, p) for p in plants]


@router.get("/{plant_id}", response_model=PlantWithLatestKpiOut)
async def get_plant(plant_id: int, repository: RepositoryDep) -> PlantWithLatestKpiOut:
    plant = await _get_plant_or_404(repository, plant_id)
    return await _with_latest(repository, plant)


@router.get("/{plant_id}/kpis/latest", response_model=KpiPointOut)
async def latest_kpi(plant_id: int, repository: RepositoryDep) -> KpiPointOut:
    await _get_plant_or_404(repository, plant_id)
    point = await repository.latest_kpi(plant_id)
    if point is None:
        raise HTTPException(status_code=404, detail=f"no KPI data for plant {plant_id} yet")
    return KpiPointOut.from_domain(point)


@router.get("/{plant_id}/kpis", response_model=list[KpiPointOut])
async def kpi_history(
    plant_id: int,
    repository: RepositoryDep,
    start: Annotated[datetime, Query(description="inclusive, ISO 8601")],
    end: Annotated[datetime, Query(description="exclusive, ISO 8601")],
    limit: Annotated[int, Query(ge=1, le=10_000)] = 1000,
) -> list[KpiPointOut]:
    # Naive timestamps are taken as UTC; stored data is always tz-aware.
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    if end <= start:
        raise HTTPException(status_code=422, detail="end must be after start")
    await _get_plant_or_404(repository, plant_id)
    points = await repository.kpi_history(plant_id, start, end, limit=limit)
    return [KpiPointOut.from_domain(p) for p in points]
