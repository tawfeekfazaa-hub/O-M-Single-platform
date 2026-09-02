from __future__ import annotations

from typing import ClassVar

import httpx

from app.adapters.base import (
    AdapterError,
    AdapterProtocolError,
    AdapterRateLimitError,
    AdapterTransientError,
    PlantInfo,
    PlantKpiReading,
    VendorAdapter,
)
from app.adapters.fusionsolar.adapter import FusionSolarAdapter
from app.adapters.fusionsolar.client import XSRF_HEADER, RealFusionSolarClient
from app.adapters.fusionsolar.mock_client import MockFusionSolarClient
from app.adapters.fusionsolar.policy import FusionSolarRatePolicy
from app.core.ratelimit import RollingWindowRateLimiter
from app.repositories.memory import InMemoryRepository
from app.scheduler.ingestion import CycleResult, IngestionScheduler
from tests.conftest import FIXED_NOON_UTC
from tests.test_fusionsolar_stationlist import StationListServer
from tests.test_fusionsolar_stationlist import make_client as make_station_list_client
from tests.test_fusionsolar_stationlist import station as station_row


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


async def test_paginated_inventory_stretches_refresh_spacing(repository: InMemoryRepository):
    # 2 station-list calls per refresh on a 4/day budget -> refreshes must
    # sit >= 2 * 86400 / 4 = 43200 s apart, whatever the configured cadence.
    client = MockFusionSolarClient(
        now=lambda: FIXED_NOON_UTC, station_list_variant="paginated", page_size=2
    )
    clock = FakeClock()
    scheduler = IngestionScheduler(
        mock_adapter(client),
        repository,
        interval_seconds=300.0,
        inventory_refresh_seconds=21_600.0,
        station_list_max_calls=4,
        station_list_window_seconds=86_400.0,
        jitter=lambda: 0.5,
        clock=clock,
    )
    result = await scheduler.run_cycle()
    assert result.inventory_refreshed and result.inventory_pages == 2
    assert client.call_counts().station_list == 2
    clock.now = 21_601.0  # past the configured 6 h cadence...
    result = await scheduler.run_cycle()
    assert not result.inventory_refreshed  # ...but inside the derived spacing
    clock.now = 43_201.0
    result = await scheduler.run_cycle()
    assert result.inventory_refreshed
    assert client.call_counts().station_list == 4


async def test_inventory_rate_limit_defers_and_never_aborts_kpi_polling(
    repository: InMemoryRepository,
):
    class InventoryLimitedAdapter(FusionSolarAdapter):
        def __init__(self) -> None:
            super().__init__(
                MockFusionSolarClient(now=lambda: FIXED_NOON_UTC),
                allow_synthetic_fields=True,
            )
            self.list_calls = 0

        async def list_plants(self) -> list[PlantInfo]:
            self.list_calls += 1
            raise AdapterRateLimitError(
                "client-side station_list budget exhausted", retry_after_seconds=43_200.0
            )

    # Seed the repository as an earlier successful refresh would have.
    await repository.upsert_plants(await mock_adapter().list_plants())
    adapter = InventoryLimitedAdapter()
    clock = FakeClock()
    scheduler = make_scheduler(adapter, repository, clock=clock)

    result = await scheduler.run_cycle()
    assert result.inventory_rate_limited and not result.inventory_refreshed
    assert result.error is None  # the cycle itself did not fail
    assert result.readings_written == 3  # KPI polling still ran
    assert scheduler.next_delay(result) == 300.0  # normal interval, no backoff
    # A stale inventory is never a complete success, and it is counted.
    assert result.inventory_stale and not result.complete_success
    assert scheduler.stats.cycles_partial == 1

    # The refresh deferred itself: no second attempt inside the window.
    clock.now = 300.0
    await scheduler.run_cycle()
    assert adapter.list_calls == 1


async def test_min_interval_floor_is_enforced(repository: InMemoryRepository):
    scheduler = IngestionScheduler(
        mock_adapter(),
        repository,
        interval_seconds=60.0,
        min_interval_seconds=330.0,
    )
    assert scheduler.interval_seconds == 330.0


class SilentOnOneStationClient(MockFusionSolarClient):
    """Lists every station but returns no KPI row for one of them."""

    SILENT = "NE=MOCK003"

    async def get_station_real_kpi(self, station_codes):
        result = await super().get_station_real_kpi(station_codes)
        result.rows = [r for r in result.rows if r["stationCode"] != self.SILENT]
        return result


async def test_partial_response_is_never_a_complete_success(
    repository: InMemoryRepository,
):
    # The station IS in the current vendor inventory, but the KPI response
    # omits it — a genuine partial answer, not a stale local row.
    client = SilentOnOneStationClient(now=lambda: FIXED_NOON_UTC)
    scheduler = make_scheduler(mock_adapter(client), repository)

    result = await scheduler.run_cycle()
    assert result.error is None
    assert result.requested_plants == 3
    assert result.readings_missing == 1
    assert result.partial
    assert not result.complete_success
    assert scheduler.stats.cycles_partial == 1


async def test_stale_plants_are_not_polled_after_the_vendor_drops_them(
    repository: InMemoryRepository,
):
    """A station removed from the account must stop consuming KPI capacity.

    Phase-1 persistence has no delete, so the row survives in the
    repository; polling it forever would report every cycle partial and
    waste KPI budget on a station the vendor will never answer for.
    """
    client = MockFusionSolarClient(now=lambda: FIXED_NOON_UTC)
    clock = FakeClock()
    scheduler = make_scheduler(
        mock_adapter(client), repository, clock=clock, inventory_refresh_seconds=3600.0
    )
    result = await scheduler.run_cycle()
    assert result.requested_plants == 3 and result.complete_success

    # The vendor drops one station from the account.
    client._stations = client._stations[:2]
    clock.now = 3601.0
    result = await scheduler.run_cycle()

    assert result.inventory_refreshed and result.plants_upserted == 2
    assert result.requested_plants == 2  # the retired station is not polled
    assert result.readings_missing == 0
    assert result.complete_success  # and the cycle is NOT reported partial
    # The row itself is still stored (no delete in the Phase-1 schema).
    assert len(await repository.list_plants()) == 3


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


async def test_incomplete_pagination_never_replaces_the_stored_inventory(
    repository: InMemoryRepository,
):
    """A truncated/contradictory station list must not become the inventory.

    End-to-end through the real client (offline MockTransport): the vendor
    claims 3 stations but the envelope is inconsistent, so the refresh fails
    and the previously stored plants are kept rather than being replaced by
    a possibly-truncated list (which would retire live plants downstream).
    """
    server = StationListServer(pages=[[station_row(1)], [station_row(2)]])
    server.total_override = 3  # promises one more station than it ever serves
    server.serve_kpi = True  # KPI polling must keep running through the failure
    client = make_station_list_client(server)
    adapter = FusionSolarAdapter(client, allow_synthetic_fields=False)

    # A previous, healthy inventory is already stored.
    await repository.upsert_plants(
        [
            PlantInfo(
                vendor="fusionsolar",
                vendor_plant_id=f"NE={i}",
                name=f"Plant {i}",
                capacity_kwp=1000.0,
                address=None,
            )
            for i in (1, 2, 3)
        ]
    )
    before = [(p.id, p.vendor_plant_id, p.name) for p in await repository.list_plants()]

    scheduler = make_scheduler(adapter, repository)
    result = await scheduler.run_cycle()

    # The failure is recorded and the cycle is NOT a complete success, but it
    # does not abort KPI polling and does not retry the inventory next cycle
    # (that would spend page 1 of the budget every cycle).
    assert result.inventory_error is not None
    assert not result.inventory_refreshed
    assert not result.complete_success
    assert result.plants_upserted == 0
    assert result.readings_written > 0  # monitoring continued
    # The stored fleet is untouched — no plant was dropped or rewritten.
    assert [(p.id, p.vendor_plant_id, p.name) for p in await repository.list_plants()] == before

    # Second cycle: the refresh is deferred, not attempted again.
    pages_before = len(server.requests)
    await scheduler.run_cycle()
    assert len(server.requests) == pages_before
    await client.close()


async def test_valid_pagination_still_refreshes_the_inventory(repository: InMemoryRepository):
    # Control case: a consistent paginated envelope updates the repository.
    server = StationListServer(pages=[[station_row(1)], [station_row(2)]])
    server.serve_kpi = True
    client = make_station_list_client(server)
    adapter = FusionSolarAdapter(client, allow_synthetic_fields=False)
    scheduler = make_scheduler(adapter, repository)

    result = await scheduler.run_cycle()

    assert result.inventory_refreshed and result.plants_upserted == 2
    assert result.inventory_pages == 2
    assert {p.vendor_plant_id for p in await repository.list_plants()} == {"NE=1", "NE=2"}
    await client.close()


async def test_spacing_reserves_a_whole_burst_in_the_rolling_window(
    repository: InMemoryRepository,
):
    """A 3-page inventory on a 4/day budget needs a FULL window of spacing.

    The budget is a rolling window, not an average allowance: at
    window x 3/4 the previous three calls still occupy slots, so only one
    page would fit and the refresh would die part-way.
    """
    client = MockFusionSolarClient(
        now=lambda: FIXED_NOON_UTC, station_list_variant="paginated", page_size=1
    )
    clock = FakeClock()
    scheduler = IngestionScheduler(
        mock_adapter(client),
        repository,
        interval_seconds=300.0,
        inventory_refresh_seconds=21_600.0,
        station_list_max_calls=4,
        station_list_window_seconds=86_400.0,
        jitter=lambda: 0.5,
        clock=clock,
    )
    result = await scheduler.run_cycle()
    assert result.inventory_pages == 3  # 3 mock stations, one per page

    clock.now = 64_801.0  # window * pages / budget — NOT enough
    assert not (await scheduler.run_cycle()).inventory_refreshed
    clock.now = 86_401.0  # a full window: the previous burst has aged out
    assert (await scheduler.run_cycle()).inventory_refreshed


async def test_inventory_spacing_counts_budget_slots_not_transport_attempts(
    repository: InMemoryRepository,
):
    # A station-list request rejected with failCode 305 retries after a
    # re-login on the slot the rejected attempt already paid for. The extra
    # HTTP attempt lands in calls_consumed but costs no budget, so pacing
    # from that counter would read a one-page refresh as a two-slot burst
    # and stretch the next refresh from 6 h to 12 h.
    class RetriedOnceAdapter(FusionSolarAdapter):
        async def list_plants(self) -> list[PlantInfo]:
            plants = await super().list_plants()
            self.last_inventory_diagnostics.calls_consumed += 1
            return plants

    adapter = RetriedOnceAdapter(
        MockFusionSolarClient(now=lambda: FIXED_NOON_UTC), allow_synthetic_fields=True
    )
    scheduler = IngestionScheduler(
        adapter,
        repository,
        station_list_max_calls=4,
        station_list_window_seconds=86_400.0,
    )
    result = await scheduler.run_cycle()
    assert result.inventory_refreshed
    assert result.inventory_pages == 1  # one logical page == one budget slot
    assert result.calls_consumed >= 2  # transport attempts still diagnosed
    assert scheduler._inventory_min_spacing == 21_600.0  # 4 bursts/day, not 2


def test_absurd_station_list_budget_derives_no_spacing(repository: InMemoryRepository):
    # A budget too large to convert to a float would raise OverflowError out
    # of the division — and run_cycle only maps AdapterError, so it would
    # escape into run_forever(). The pre-flight check reports such a value as
    # a config error; here it simply derives no spacing.
    scheduler = IngestionScheduler(
        mock_adapter(),
        repository,
        station_list_max_calls=int("1" * 1000),
        station_list_window_seconds=86_400.0,
    )
    assert scheduler._derive_inventory_spacing(1) == 0.0


async def test_a_growing_inventory_waits_for_the_earlier_bursts_to_expire(
    repository: InMemoryRepository,
):
    # The pacing formula assumes equally sized bursts. On the 4/day default a
    # 1-page refresh at hour 0 and a 2-page one at hour 6 leave only ONE free
    # slot at hour 18, so the paced 2-page burst would take page 1 and be
    # rate-limited on page 2 — the refresh fails and defers a full window
    # while the inventory goes stale.
    hour = 3600.0
    clock = FakeClock()
    server = StationListServer([[station_row(0)]])
    client = make_station_list_client(server)
    adapter = FusionSolarAdapter(client, allow_synthetic_fields=True)
    scheduler = IngestionScheduler(
        adapter,
        repository,
        station_list_max_calls=4,
        station_list_window_seconds=24 * hour,
        clock=clock,
    )

    await scheduler._refresh_inventory(CycleResult())
    assert scheduler._inventory_min_spacing == 6 * hour  # 4 one-page bursts/day

    # The fleet grows past one page.
    clock.now = 6 * hour
    server.pages = [[station_row(i) for i in range(100)], [station_row(100)]]
    await scheduler._refresh_inventory(CycleResult())
    # Pacing alone would say 12 h (hour 18), when the hour-0 call is still
    # held: three of four slots are occupied and a 2-page burst cannot run.
    assert scheduler._derive_inventory_spacing(2) == 12 * hour
    assert scheduler._inventory_min_spacing == 18 * hour  # hour 24: hour 0 expired

    clock.now = 18 * hour
    assert not scheduler._inventory_due()
    clock.now = 24 * hour
    assert scheduler._inventory_due()

    # Back to a steady sequence of equal bursts, the pacing rule takes over.
    await scheduler._refresh_inventory(CycleResult())
    assert scheduler._inventory_min_spacing == 12 * hour
    await client.close()


async def test_a_transient_relogin_failure_aborts_the_cycle_too(
    repository: InMemoryRepository,
):
    # The session can fail to establish for reasons other than a throttle: a
    # re-login after failCode 305 can time out or answer 5xx. Deferring only
    # the refresh and polling on would log in again into the same outage,
    # spending another scarce login slot each cycle.
    class Server:
        def __init__(self) -> None:
            self.paths: list[str] = []
            self.logins = 0
            self.expire_once = True

        def handler(self, request: httpx.Request) -> httpx.Response:
            path = request.url.path.removeprefix("/thirdData")
            self.paths.append(path)
            if path == "/login":
                self.logins += 1
                if self.logins >= 2:
                    return httpx.Response(503, json={})  # vendor outage
                return httpx.Response(
                    200, json={"success": True, "failCode": 0}, headers={XSRF_HEADER: "tok"}
                )
            if path == "/getStationList":
                if self.expire_once:
                    self.expire_once = False
                    return httpx.Response(200, json={"success": False, "failCode": 305})
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "failCode": 0,
                        "data": {
                            "list": [station_row(1)],
                            "pageNo": 1,
                            "pageSize": 100,
                            "pageCount": 1,
                            "total": 1,
                        },
                    },
                )
            raise AssertionError("KPI polling must not run after a failed session")

    server = Server()
    client = RealFusionSolarClient(
        base_url="https://fake.fusionsolar.example/thirdData",
        username="nb-user",
        system_code="nb-system-code",
        policy=FusionSolarRatePolicy(
            login_max_calls=10, station_list_max_calls=4, station_list_window_seconds=86_400.0
        ),
        transport=httpx.MockTransport(server.handler),
    )
    await repository.upsert_plants(
        [PlantInfo(vendor="fusionsolar", vendor_plant_id="NE=1", name="x")]
    )
    scheduler = IngestionScheduler(
        FusionSolarAdapter(client, allow_synthetic_fields=True),
        repository,
        station_list_max_calls=4,
        station_list_window_seconds=86_400.0,
    )

    result = await scheduler.run_cycle()
    assert server.logins == 2  # the failed one, and no third
    assert result.transient and result.error is not None
    await client.close()


async def test_a_throttled_relogin_aborts_the_cycle_instead_of_polling_on(
    repository: InMemoryRepository,
):
    # A station-list call answered with failCode 305 re-logins, and that
    # login can come back 429. Treating it as a station-list throttle would
    # defer the inventory and carry on into KPI polling, which finds no
    # token and logs in AGAIN — a third request to the endpoint the vendor
    # had just throttled, inside its own Retry-After.
    class Server:
        def __init__(self) -> None:
            self.paths: list[str] = []
            self.logins = 0
            self.expire_once = True

        def handler(self, request: httpx.Request) -> httpx.Response:
            path = request.url.path.removeprefix("/thirdData")
            self.paths.append(path)
            if path == "/login":
                self.logins += 1
                if self.logins >= 2:  # the vendor throttles the re-login
                    return httpx.Response(429, headers={"Retry-After": "600"}, json={})
                return httpx.Response(
                    200, json={"success": True, "failCode": 0}, headers={XSRF_HEADER: "tok"}
                )
            if path == "/getStationList":
                if self.expire_once:
                    self.expire_once = False
                    return httpx.Response(200, json={"success": False, "failCode": 305})
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "failCode": 0,
                        "data": {
                            "list": [station_row(1)],
                            "pageNo": 1,
                            "pageSize": 100,
                            "pageCount": 1,
                            "total": 1,
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "failCode": 0,
                    "data": [],
                    "params": {"currentTime": 1_780_000_000_000},
                },
            )

    server = Server()
    client = RealFusionSolarClient(
        base_url="https://fake.fusionsolar.example/thirdData",
        username="nb-user",
        system_code="nb-system-code",
        policy=FusionSolarRatePolicy(
            login_max_calls=10, station_list_max_calls=4, station_list_window_seconds=86_400.0
        ),
        transport=httpx.MockTransport(server.handler),
    )
    await repository.upsert_plants(
        [PlantInfo(vendor="fusionsolar", vendor_plant_id="NE=1", name="x")]
    )
    scheduler = IngestionScheduler(
        FusionSolarAdapter(client, allow_synthetic_fields=True),
        repository,
        station_list_max_calls=4,
        station_list_window_seconds=86_400.0,
    )

    result = await scheduler.run_cycle()
    assert server.logins == 2  # the throttled one, and no third
    assert server.paths == ["/login", "/getStationList", "/login"]
    assert result.error is not None  # the whole cycle backed off
    assert result.retry_after_seconds == 600.0  # on the vendor's own delay
    await client.close()


async def test_a_capacity_hint_is_honoured_instead_of_a_full_window(
    repository: InMemoryRepository,
):
    # The pre-flight check measures when EVERY page of the next attempt can
    # run, so widening it to a full window only adds staleness. Here a
    # 2-page fleet refreshed at hour 0 advertises 3 pages at hour 12: the
    # hour-0 calls expire at hour 24, leaving exactly the three slots the
    # retry needs. The old full-window rule pushed it to hour 36.
    hour = 3600.0
    clock = FakeClock()
    server = StationListServer([[station_row(i) for i in range(100)], [station_row(100)]])
    server.serve_kpi = True
    client = make_station_list_client(server, station_list_max_calls=4, clock=clock)
    scheduler = IngestionScheduler(
        FusionSolarAdapter(client, allow_synthetic_fields=True),
        repository,
        station_list_max_calls=4,
        station_list_window_seconds=24 * hour,
        clock=clock,
    )
    assert (await scheduler.run_cycle()).inventory_refreshed

    clock.now = 12 * hour
    server.pages = [
        [station_row(i) for i in range(100)],
        [station_row(i) for i in range(100, 200)],
        [station_row(200)],
    ]
    result = await scheduler.run_cycle()
    assert result.inventory_rate_limited  # deferred, not half-run
    assert scheduler._inventory_not_before == 24 * hour  # not 36

    clock.now = 24 * hour
    assert (await scheduler.run_cycle()).inventory_refreshed
    await client.close()


async def test_a_plain_rate_limit_still_waits_a_full_window(repository: InMemoryRepository):
    # A hint that only frees ONE slot must still be widened: retrying on it
    # would spend that slot and fail on the same page forever.
    hour = 3600.0
    clock = FakeClock()

    class RateLimitedAdapter(FusionSolarAdapter):
        async def list_plants(self) -> list[PlantInfo]:
            raise AdapterRateLimitError("budget exhausted", retry_after_seconds=hour)

    scheduler = IngestionScheduler(
        RateLimitedAdapter(
            MockFusionSolarClient(now=lambda: FIXED_NOON_UTC), allow_synthetic_fields=True
        ),
        repository,
        station_list_max_calls=4,
        station_list_window_seconds=24 * hour,
        clock=clock,
    )
    await scheduler.run_cycle()
    assert scheduler._inventory_not_before == 24 * hour


async def test_a_failed_refresh_defers_until_the_calls_it_spent_expire(
    repository: InMemoryRepository,
):
    # A refresh that dies on page 3 has already paid for three slots and the
    # rolling limiter keeps holding them. Retrying on the plain 6 h cadence
    # would spend another call into a budget that cannot carry the burst,
    # be rejected, and leave the inventory stale even longer.
    hour = 3600.0
    clock = FakeClock()
    server = StationListServer(
        [[station_row(i) for i in range(100)], [station_row(100)], [station_row(101)]]
    )
    server.serve_kpi = True
    server.page_size_per_page = {3: 50}  # contract violation, discovered on page 3

    client = make_station_list_client(server)
    scheduler = IngestionScheduler(
        FusionSolarAdapter(client, allow_synthetic_fields=True),
        repository,
        station_list_max_calls=4,
        station_list_window_seconds=24 * hour,
        clock=clock,
    )

    result = await scheduler.run_cycle()
    assert result.inventory_error is not None  # recorded, not raised
    assert len(server.requests) == 3  # three slots spent before the violation

    clock.now = 6 * hour  # the plain cadence would retry here
    assert not scheduler._inventory_due()
    clock.now = 24 * hour  # the three calls have now expired
    assert scheduler._inventory_due()
    await client.close()


def test_burst_history_is_pruned_to_the_rolling_window(repository: InMemoryRepository):
    # Only the calls the vendor's limiter still holds may constrain the next
    # refresh; anything older has expired and must not grow without bound.
    scheduler = IngestionScheduler(
        mock_adapter(),
        repository,
        station_list_max_calls=4,
        station_list_window_seconds=86_400.0,
    )
    for at in (0.0, 43_200.0, 86_400.0, 129_600.0):
        scheduler._record_inventory_burst(at, 1)
    assert list(scheduler._inventory_bursts) == [(86_400.0, 1), (129_600.0, 1)]


def test_derived_spacing_counts_complete_bursts_per_window(
    repository: InMemoryRepository,
):
    # window / floor(budget / pages): only whole refreshes fit a rolling
    # window, so the spacing must never be an average rate.
    scheduler = IngestionScheduler(
        mock_adapter(),
        repository,
        station_list_max_calls=4,
        station_list_window_seconds=86_400.0,
    )
    assert scheduler._derive_inventory_spacing(1) == 21_600.0  # 4 bursts/day
    assert scheduler._derive_inventory_spacing(2) == 43_200.0  # 2 bursts/day
    assert scheduler._derive_inventory_spacing(3) == 86_400.0  # 1 burst/day
    assert scheduler._derive_inventory_spacing(4) == 86_400.0

    # A budget that is not a multiple of the page count must round DOWN:
    # 5 calls / 2 pages would drift to 9.6 h on an average-rate formula and
    # need six slots inside one window.
    uneven = IngestionScheduler(
        mock_adapter(),
        repository,
        station_list_max_calls=5,
        station_list_window_seconds=86_400.0,
    )
    assert uneven._derive_inventory_spacing(2) == 43_200.0  # floor(5/2) = 2


async def test_uneven_budget_spacing_never_exhausts_the_rolling_window(
    repository: InMemoryRepository,
):
    # Three consecutive 2-page refreshes on a 5-call/24 h budget must each
    # get both of their slots.
    client = MockFusionSolarClient(
        now=lambda: FIXED_NOON_UTC, station_list_variant="paginated", page_size=2
    )
    clock = FakeClock()
    scheduler = IngestionScheduler(
        mock_adapter(client),
        repository,
        interval_seconds=300.0,
        inventory_refresh_seconds=0.0,
        station_list_max_calls=5,
        station_list_window_seconds=86_400.0,
        jitter=lambda: 0.5,
        clock=clock,
    )
    limiter = RollingWindowRateLimiter(5, 86_400.0, clock=clock)
    refreshes = 0
    for tick in range(0, 90_000, 3_600):
        clock.now = float(tick)
        if scheduler._inventory_due():
            for _ in range(2):  # the two station-list calls of this refresh
                await limiter.acquire(wait=False)  # raises if the window is full
            await scheduler.run_cycle()
            refreshes += 1
    assert refreshes == 3  # t=0, 43200, 86400 — each with both slots free


async def test_rate_limited_refresh_defers_a_full_window(repository: InMemoryRepository):
    """A partial burst must not be retried while its own calls still count.

    The limiter's hint frees one slot; retrying then would resend the same
    partial burst and fail on the same page forever.
    """

    class InventoryLimitedAdapter(FusionSolarAdapter):
        def __init__(self) -> None:
            super().__init__(
                MockFusionSolarClient(now=lambda: FIXED_NOON_UTC), allow_synthetic_fields=True
            )
            self.list_calls = 0

        async def list_plants(self) -> list[PlantInfo]:
            self.list_calls += 1
            raise AdapterRateLimitError("budget exhausted", retry_after_seconds=100.0)

    await repository.upsert_plants(await mock_adapter().list_plants())
    adapter = InventoryLimitedAdapter()
    clock = FakeClock()
    scheduler = IngestionScheduler(
        adapter,
        repository,
        interval_seconds=300.0,
        station_list_max_calls=4,
        station_list_window_seconds=86_400.0,
        jitter=lambda: 0.5,
        clock=clock,
    )
    assert (await scheduler.run_cycle()).inventory_rate_limited

    clock.now = 200.0  # past the limiter's own 100 s hint...
    await scheduler.run_cycle()
    assert adapter.list_calls == 1  # ...but the burst has not expired yet

    clock.now = 86_401.0  # a full window later the whole burst has aged out
    await scheduler.run_cycle()
    assert adapter.list_calls == 2


async def test_min_interval_floor_also_applies_to_failure_backoff(
    repository: InMemoryRepository,
):
    # Without the floor the first backoff (60 s) would fire well inside the
    # KPI window and just hit the client-side limiter again.
    scheduler = IngestionScheduler(
        FailingAdapter(AdapterError("boom")),
        repository,
        interval_seconds=300.0,
        min_interval_seconds=330.0,
        backoff_base_seconds=60.0,
        jitter=lambda: 0.5,
    )
    result = await scheduler.run_cycle()
    assert scheduler.next_delay(result) == 330.0
    # Once the backoff outgrows the floor it wins again.
    for _ in range(4):
        result = await scheduler.run_cycle()
    assert scheduler.next_delay(result) == 960.0


async def test_retry_after_is_never_scaled_down_by_jitter(repository: InMemoryRepository):
    # A vendor Retry-After is a hard lower bound: retrying at 0.75x would
    # send the next request before the server's requested delay.
    scheduler = IngestionScheduler(
        FailingAdapter(AdapterRateLimitError("429", retry_after_seconds=600.0)),
        repository,
        interval_seconds=300.0,
        backoff_base_seconds=60.0,
        jitter=lambda: 0.0,  # worst case: 0.75x multiplier
    )
    result = await scheduler.run_cycle()
    assert scheduler.next_delay(result) == 600.0

    # Jitter still applies to the backoff itself when it is the larger value.
    jittered = IngestionScheduler(
        FailingAdapter(AdapterError("boom")),
        repository,
        interval_seconds=300.0,
        backoff_base_seconds=1000.0,
        jitter=lambda: 0.0,
    )
    result = await jittered.run_cycle()
    assert jittered.next_delay(result) == 750.0  # 1000 * 0.75


async def test_restart_without_a_snapshot_polls_provisionally(repository: InMemoryRepository):
    """A restart must not black out monitoring, and must say so.

    With a persistent repository the scheduler starts with no confirmed
    inventory. If the first station-list refresh is rate-limited, KPI
    polling still runs against the persisted plants (an O&M blackout would
    be far worse than briefly polling a retired station) and the cycle is
    flagged provisional so its "missing" count is not read as a data fault.
    """

    class InventoryLimitedAdapter(FusionSolarAdapter):
        def __init__(self) -> None:
            super().__init__(
                MockFusionSolarClient(now=lambda: FIXED_NOON_UTC), allow_synthetic_fields=True
            )

        async def list_plants(self) -> list[PlantInfo]:
            raise AdapterRateLimitError("budget exhausted", retry_after_seconds=86_400.0)

    await repository.upsert_plants(
        [
            PlantInfo(
                vendor="fusionsolar",
                vendor_plant_id=code,
                name=code,
                capacity_kwp=1000.0,
                address=None,
            )
            for code in ("NE=MOCK001", "NE=MOCK002", "NE=MOCK003", "NE=RETIRED")
        ]
    )
    scheduler = make_scheduler(InventoryLimitedAdapter(), repository)
    result = await scheduler.run_cycle()

    assert result.inventory_rate_limited and result.inventory_provisional
    assert result.requested_plants == 4  # monitoring continues
    assert result.readings_written == 3  # the retired station answers nothing

    # A confirmed inventory clears the flag and the retired station with it.
    scheduler._adapter = mock_adapter()
    scheduler._inventory_not_before = None
    result = await scheduler.run_cycle()
    assert result.inventory_refreshed and not result.inventory_provisional
    assert result.requested_plants == 3 and result.complete_success


async def test_guard_exceeded_inventory_defers_instead_of_retrying_every_cycle(
    repository: InMemoryRepository,
):
    """An inventory too large for the budget must not be retried every cycle.

    The guard raises after ONE call by design; retrying it on the next cycle
    would spend page 1 of the station-list budget over and over until the
    window is exhausted, and aborting the cycle would stop KPI monitoring
    with it.
    """

    class GuardFailAdapter(FusionSolarAdapter):
        def __init__(self) -> None:
            super().__init__(
                MockFusionSolarClient(now=lambda: FIXED_NOON_UTC), allow_synthetic_fields=True
            )
            self.list_calls = 0

        async def list_plants(self) -> list[PlantInfo]:
            self.list_calls += 1
            raise AdapterProtocolError(
                "station list needs 5 pages but the effective guard is 4; "
                "raise FUSIONSOLAR_STATION_LIST_MAX_CALLS"
            )

    await repository.upsert_plants(await mock_adapter().list_plants())
    adapter = GuardFailAdapter()
    clock = FakeClock()
    scheduler = make_scheduler(adapter, repository, clock=clock)

    result = await scheduler.run_cycle()
    assert result.inventory_error is not None and not result.complete_success
    assert result.error is None  # the cycle itself did not abort
    assert result.readings_written == 3  # KPI polling continued
    assert scheduler.next_delay(result) == 300.0  # normal interval, no backoff

    for tick in (300.0, 600.0, 900.0):
        clock.now = tick
        await scheduler.run_cycle()
    assert adapter.list_calls == 1  # deferred, not retried every cycle

    clock.now = 21_601.0  # after the inventory cadence it may try again
    await scheduler.run_cycle()
    assert adapter.list_calls == 2


async def test_stale_inventory_is_reported_for_the_whole_deferral(
    repository: InMemoryRepository,
):
    """Every cycle of the deferral window runs on the SAME stale inventory.

    The flags describe the current inventory, not just the attempt that
    failed, so a cycle suppressed by the deferral must not look healthy to
    a health consumer.
    """

    class InventoryLimitedAdapter(FusionSolarAdapter):
        def __init__(self) -> None:
            super().__init__(
                MockFusionSolarClient(now=lambda: FIXED_NOON_UTC), allow_synthetic_fields=True
            )
            self.fail = True

        async def list_plants(self) -> list[PlantInfo]:
            if self.fail:
                raise AdapterRateLimitError("budget exhausted", retry_after_seconds=86_400.0)
            return await super().list_plants()

    await repository.upsert_plants(await mock_adapter().list_plants())
    adapter = InventoryLimitedAdapter()
    clock = FakeClock()
    scheduler = IngestionScheduler(
        adapter,
        repository,
        interval_seconds=300.0,
        station_list_max_calls=4,
        station_list_window_seconds=86_400.0,
        jitter=lambda: 0.5,
        clock=clock,
    )

    first = await scheduler.run_cycle()
    assert first.inventory_rate_limited and not first.complete_success

    # Cycles suppressed by the deferral report the same stale state.
    for tick in (300.0, 600.0, 900.0):
        clock.now = tick
        result = await scheduler.run_cycle()
        assert not result.inventory_refreshed
        assert result.inventory_rate_limited and result.inventory_stale
        assert not result.complete_success
        assert result.readings_written == 3  # monitoring never stopped
    assert scheduler.stats.cycles_partial == 4  # every stale cycle counted

    # A successful refresh clears it.
    adapter.fail = False
    clock.now = 86_401.0
    healthy = await scheduler.run_cycle()
    assert healthy.inventory_refreshed and not healthy.inventory_stale
    assert healthy.complete_success


async def test_inventory_failure_state_also_survives_the_deferral(
    repository: InMemoryRepository,
):
    class GuardFailAdapter(FusionSolarAdapter):
        def __init__(self) -> None:
            super().__init__(
                MockFusionSolarClient(now=lambda: FIXED_NOON_UTC), allow_synthetic_fields=True
            )

        async def list_plants(self) -> list[PlantInfo]:
            raise AdapterProtocolError("station list needs more pages than the guard allows")

    await repository.upsert_plants(await mock_adapter().list_plants())
    clock = FakeClock()
    scheduler = make_scheduler(GuardFailAdapter(), repository, clock=clock)

    first = await scheduler.run_cycle()
    assert first.inventory_error is not None

    clock.now = 300.0
    deferred = await scheduler.run_cycle()
    assert not deferred.inventory_refreshed
    assert deferred.inventory_error is not None and not deferred.complete_success
