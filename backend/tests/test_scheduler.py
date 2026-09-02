from __future__ import annotations

from typing import ClassVar

from app.adapters.base import (
    AdapterError,
    AdapterRateLimitError,
    AdapterTransientError,
    PlantInfo,
    PlantKpiReading,
    VendorAdapter,
)
from app.adapters.fusionsolar.adapter import FusionSolarAdapter
from app.adapters.fusionsolar.mock_client import MockFusionSolarClient
from app.repositories.memory import InMemoryRepository
from app.scheduler.ingestion import IngestionScheduler
from tests.conftest import FIXED_NOON_UTC


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


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


def mock_adapter(client: MockFusionSolarClient | None = None) -> FusionSolarAdapter:
    client = client or MockFusionSolarClient(now=lambda: FIXED_NOON_UTC)
    return FusionSolarAdapter(client, allow_synthetic_fields=True)


def make_scheduler(
    adapter: VendorAdapter,
    repository: InMemoryRepository,
    *,
    clock: FakeClock | None = None,
    inventory_refresh_seconds: float = 21_600.0,
) -> IngestionScheduler:
    # jitter pinned to 0.5 -> multiplier exactly 1.0, delays deterministic.
    return IngestionScheduler(
        adapter,
        repository,
        interval_seconds=300.0,
        inventory_refresh_seconds=inventory_refresh_seconds,
        backoff_base_seconds=60.0,
        backoff_max_seconds=1800.0,
        jitter=lambda: 0.5,
        clock=clock or FakeClock(),
    )


async def test_cycle_ingests_mock_data_end_to_end(repository: InMemoryRepository):
    scheduler = make_scheduler(mock_adapter(), repository)
    result = await scheduler.run_cycle()
    assert result.error is None
    assert result.inventory_refreshed and result.plants_upserted == 3
    assert result.requested_plants == 3
    assert result.readings_returned == 3 and result.readings_written == 3
    assert result.complete_success
    plants = await repository.list_plants()
    assert len(plants) == 3
    for plant in plants:
        assert await repository.latest_kpi(plant.id) is not None
    assert scheduler.next_delay(result) == 300.0


async def test_inventory_is_not_fetched_every_kpi_cycle(repository: InMemoryRepository):
    client = MockFusionSolarClient(now=lambda: FIXED_NOON_UTC)
    clock = FakeClock()
    scheduler = make_scheduler(mock_adapter(client), repository, clock=clock)
    for i in range(5):
        clock.now = i * 300.0  # five KPI cycles well within the 6h cadence
        result = await scheduler.run_cycle()
        assert result.error is None
    counts = client.call_counts()
    assert counts.station_list == 1  # inventory fetched exactly once
    assert counts.station_real_kpi == 5  # KPIs fetched every cycle


async def test_inventory_refreshes_after_cadence_elapses(repository: InMemoryRepository):
    client = MockFusionSolarClient(now=lambda: FIXED_NOON_UTC)
    clock = FakeClock()
    scheduler = make_scheduler(
        mock_adapter(client), repository, clock=clock, inventory_refresh_seconds=3600.0
    )
    await scheduler.run_cycle()
    clock.now = 3599.0
    result = await scheduler.run_cycle()
    assert not result.inventory_refreshed
    clock.now = 3601.0
    result = await scheduler.run_cycle()
    assert result.inventory_refreshed
    assert client.call_counts().station_list == 2


async def test_min_interval_floor_is_enforced(repository: InMemoryRepository):
    scheduler = IngestionScheduler(
        mock_adapter(),
        repository,
        interval_seconds=60.0,
        min_interval_seconds=330.0,
    )
    assert scheduler.interval_seconds == 330.0


async def test_partial_response_is_never_a_complete_success(
    repository: InMemoryRepository,
):
    # Mock returns rows only for stations it knows; unknown code -> missing.
    client = MockFusionSolarClient(now=lambda: FIXED_NOON_UTC)
    adapter = mock_adapter(client)
    scheduler = make_scheduler(adapter, repository)
    await scheduler.run_cycle()

    # Inject a plant the vendor will not answer for.
    from app.adapters.base import PlantInfo

    await repository.upsert_plants(
        [
            PlantInfo(
                vendor="fusionsolar",
                vendor_plant_id="NE=GHOST",
                name="Ghost",
                capacity_kwp=1.0,
                address=None,
            )
        ]
    )
    result = await scheduler.run_cycle()
    assert result.error is None
    assert result.readings_missing == 1
    assert result.partial
    assert not result.complete_success
    assert scheduler.stats.cycles_partial == 1


async def test_rate_limit_error_triggers_backoff(repository: InMemoryRepository):
    error = AdapterRateLimitError("407", retry_after_seconds=500.0)
    scheduler = make_scheduler(FailingAdapter(error), repository)

    result = await scheduler.run_cycle()
    assert result.rate_limited and result.error is not None
    assert scheduler.stats.consecutive_failures == 1
    # retry_after (500) exceeds first backoff step (60) -> it wins.
    assert scheduler.next_delay(result) == 500.0


async def test_transient_error_backs_off_without_immediate_retry(
    repository: InMemoryRepository,
):
    scheduler = make_scheduler(FailingAdapter(AdapterTransientError("timeout")), repository)
    result = await scheduler.run_cycle()
    assert result.transient and result.error is not None
    assert scheduler.stats.cycles_total == 1  # exactly one attempt, no retry
    assert scheduler.next_delay(result) == 60.0  # backoff, not instant


async def test_backoff_grows_exponentially_and_caps(repository: InMemoryRepository):
    scheduler = make_scheduler(FailingAdapter(AdapterError("boom")), repository)
    delays = []
    for _ in range(7):
        result = await scheduler.run_cycle()
        delays.append(scheduler.next_delay(result))
    assert delays == [60.0, 120.0, 240.0, 480.0, 960.0, 1800.0, 1800.0]
    assert scheduler.stats.cycles_failed == 7


async def test_success_resets_failure_streak(repository: InMemoryRepository):
    scheduler = make_scheduler(FailingAdapter(AdapterError("boom")), repository)
    await scheduler.run_cycle()
    assert scheduler.stats.consecutive_failures == 1
    scheduler._adapter = mock_adapter()  # recovery
    result = await scheduler.run_cycle()
    assert result.error is None
    assert scheduler.stats.consecutive_failures == 0
    assert scheduler.next_delay(result) == 300.0


async def test_cycle_never_raises(repository: InMemoryRepository):
    scheduler = make_scheduler(FailingAdapter(AdapterError("boom")), repository)
    result = await scheduler.run_cycle()  # must not raise
    assert result.error == "boom"


async def test_diagnostics_expose_counts_only(repository: InMemoryRepository):
    scheduler = make_scheduler(mock_adapter(), repository)
    result = await scheduler.run_cycle()
    # Every diagnostic field is a number/bool/None — no identifiers.
    for name in (
        "inventory_pages",
        "plants_upserted",
        "requested_plants",
        "readings_returned",
        "readings_written",
        "readings_missing",
        "readings_duplicate",
        "readings_unexpected",
        "invalid_values",
        "batches",
        "calls_consumed",
    ):
        assert isinstance(getattr(result, name), int)
