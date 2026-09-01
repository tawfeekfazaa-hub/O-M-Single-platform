from __future__ import annotations

from typing import ClassVar

from app.adapters.base import (
    AdapterError,
    AdapterRateLimitError,
    PlantInfo,
    PlantKpiReading,
    VendorAdapter,
)
from app.adapters.fusionsolar.adapter import FusionSolarAdapter
from app.repositories.memory import InMemoryRepository
from app.scheduler.ingestion import IngestionScheduler


class FailingAdapter(VendorAdapter):
    vendor: ClassVar[str] = "failing"

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def authenticate(self) -> None:
        raise self.error

    async def list_plants(self) -> list[PlantInfo]:
        return []

    async def fetch_plant_kpis(self, vendor_plant_ids: list[str]) -> list[PlantKpiReading]:
        return []

    async def health_check(self) -> bool:
        return False


def make_scheduler(adapter: VendorAdapter, repository: InMemoryRepository) -> IngestionScheduler:
    # jitter pinned to 0.5 -> multiplier exactly 1.0, delays are deterministic.
    return IngestionScheduler(
        adapter,
        repository,
        interval_seconds=300.0,
        backoff_base_seconds=60.0,
        backoff_max_seconds=1800.0,
        jitter=lambda: 0.5,
    )


async def test_cycle_ingests_mock_data_end_to_end(
    adapter: FusionSolarAdapter, repository: InMemoryRepository
):
    scheduler = make_scheduler(adapter, repository)
    result = await scheduler.run_cycle()
    assert result.error is None
    assert result.plants_upserted == 3
    assert result.readings_written == 3
    plants = await repository.list_plants()
    assert len(plants) == 3
    for plant in plants:
        assert await repository.latest_kpi(plant.id) is not None
    assert scheduler.next_delay(result) == 300.0
    assert scheduler.stats.consecutive_failures == 0


async def test_rate_limit_error_triggers_backoff(repository: InMemoryRepository):
    error = AdapterRateLimitError("407", retry_after_seconds=500.0)
    scheduler = make_scheduler(FailingAdapter(error), repository)

    result = await scheduler.run_cycle()
    assert result.rate_limited and result.error is not None
    assert scheduler.stats.consecutive_failures == 1
    # retry_after (500) exceeds first backoff step (60) -> it wins.
    assert scheduler.next_delay(result) == 500.0


async def test_backoff_grows_exponentially_and_caps(repository: InMemoryRepository):
    scheduler = make_scheduler(FailingAdapter(AdapterError("boom")), repository)
    delays = []
    for _ in range(7):
        result = await scheduler.run_cycle()
        delays.append(scheduler.next_delay(result))
    assert delays == [60.0, 120.0, 240.0, 480.0, 960.0, 1800.0, 1800.0]
    assert scheduler.stats.cycles_failed == 7


async def test_success_resets_failure_streak(
    adapter: FusionSolarAdapter, repository: InMemoryRepository
):
    scheduler = make_scheduler(FailingAdapter(AdapterError("boom")), repository)
    await scheduler.run_cycle()
    assert scheduler.stats.consecutive_failures == 1
    scheduler._adapter = adapter  # recovery
    result = await scheduler.run_cycle()
    assert result.error is None
    assert scheduler.stats.consecutive_failures == 0
    assert scheduler.next_delay(result) == 300.0


async def test_cycle_never_raises(repository: InMemoryRepository):
    scheduler = make_scheduler(FailingAdapter(AdapterError("boom")), repository)
    result = await scheduler.run_cycle()  # must not raise
    assert result.error == "boom"
