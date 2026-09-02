from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.adapters.base import PlantInfo, PlantKpiReading, PlantStatus
from app.repositories.memory import InMemoryRepository

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def plant_info(pid: str = "NE=1", name: str = "P1") -> PlantInfo:
    return PlantInfo(
        vendor="fusionsolar", vendor_plant_id=pid, name=name, capacity_kwp=100.0, address="A"
    )


def reading(pid: str = "NE=1", ts: datetime = T0, power: float = 42.0) -> PlantKpiReading:
    return PlantKpiReading(
        vendor="fusionsolar",
        vendor_plant_id=pid,
        ts=ts,
        active_power_kw=power,
        daily_energy_kwh=power * 3,
        total_energy_kwh=1_000_000.0,
        performance_ratio=0.8,
        status=PlantStatus.HEALTHY,
    )


async def test_upsert_is_idempotent_and_updates_metadata(repository: InMemoryRepository):
    (first,) = await repository.upsert_plants([plant_info(name="Old name")])
    (second,) = await repository.upsert_plants([plant_info(name="New name")])
    assert first.id == second.id
    assert (await repository.get_plant(first.id)).name == "New name"
    assert len(await repository.list_plants()) == 1


async def test_record_kpis_updates_plant_status(repository: InMemoryRepository):
    (plant,) = await repository.upsert_plants([plant_info()])
    assert plant.status is PlantStatus.UNKNOWN
    written = await repository.record_kpis([reading()])
    assert written == 1
    assert (await repository.get_plant(plant.id)).status is PlantStatus.HEALTHY
    latest = await repository.latest_kpi(plant.id)
    assert latest is not None and latest.active_power_kw == 42.0


async def test_readings_for_unknown_plants_are_skipped(repository: InMemoryRepository):
    written = await repository.record_kpis([reading(pid="NE=unknown")])
    assert written == 0


async def test_duplicate_timestamp_is_not_double_stored(repository: InMemoryRepository):
    (plant,) = await repository.upsert_plants([plant_info()])
    assert await repository.record_kpis([reading()]) == 1
    assert await repository.record_kpis([reading()]) == 0
    history = await repository.kpi_history(
        plant.id, T0 - timedelta(hours=1), T0 + timedelta(hours=1)
    )
    assert len(history) == 1


async def test_history_window_is_inclusive_exclusive(repository: InMemoryRepository):
    (plant,) = await repository.upsert_plants([plant_info()])
    times = [T0 + timedelta(minutes=5 * i) for i in range(4)]
    await repository.record_kpis([reading(ts=t, power=float(i)) for i, t in enumerate(times)])
    window = await repository.kpi_history(plant.id, times[1], times[3])
    assert [p.ts for p in window] == [times[1], times[2]]
    assert [p.active_power_kw for p in window] == [1.0, 2.0]


async def test_per_plant_isolation(repository: InMemoryRepository):
    p1, p2 = await repository.upsert_plants([plant_info("NE=1"), plant_info("NE=2", name="P2")])
    await repository.record_kpis([reading("NE=1", power=10.0), reading("NE=2", power=20.0)])
    assert (await repository.latest_kpi(p1.id)).active_power_kw == 10.0
    assert (await repository.latest_kpi(p2.id)).active_power_kw == 20.0
    only_p1 = await repository.kpi_history(p1.id, T0 - timedelta(days=1), T0 + timedelta(days=1))
    assert all(p.plant_id == p1.id for p in only_p1)


async def test_an_unreported_capacity_never_erases_a_stored_one():
    # The adapter reports capacity None both when the vendor omits it and
    # when the value it sent could not be read. Writing that over a good
    # stored value is silent metadata loss, so a missing capacity means "no
    # update" rather than "unknown now".
    repo = InMemoryRepository()
    await repo.upsert_plants(
        [PlantInfo(vendor="fusionsolar", vendor_plant_id="NE=1", name="P", capacity_kwp=2500.0)]
    )
    await repo.upsert_plants(
        [PlantInfo(vendor="fusionsolar", vendor_plant_id="NE=1", name="P", capacity_kwp=None)]
    )
    (plant,) = await repo.list_plants()
    assert plant.capacity_kwp == 2500.0

    # A capacity the vendor DOES report still updates the stored one.
    await repo.upsert_plants(
        [PlantInfo(vendor="fusionsolar", vendor_plant_id="NE=1", name="P", capacity_kwp=3100.0)]
    )
    (plant,) = await repo.list_plants()
    assert plant.capacity_kwp == 3100.0
