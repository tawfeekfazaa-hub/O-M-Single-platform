"""Adapter-level normalization tests: units, finite validation, PR
normalization, health mapping, timestamp provenance, count diagnostics,
and the synthetic-fields boundary between mock and real mapping."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.adapters.base import (
    AdapterTransientError,
    PlantInfo,
    PlantKpiReading,
    PlantStatus,
)
from app.adapters.fusionsolar.adapter import (
    FusionSolarAdapter,
    KpiDiagnostics,
    _finite_float,
    _health_status,
    normalize_performance_ratio,
)
from app.adapters.fusionsolar.client import ClientCallCounts, KpiBatchResult, StationListResult
from app.repositories.memory import InMemoryRepository

VENDOR_MS = 1_780_000_000_000


class ScriptedClient:
    """Offline stand-in that returns scripted vendor-shaped rows."""

    def __init__(
        self,
        kpi_rows: list[dict[str, Any]],
        *,
        vendor_ms: int | None = VENDOR_MS,
        stations: list[dict[str, Any]] | None = None,
    ) -> None:
        self._kpi_rows = kpi_rows
        self._vendor_ms = vendor_ms
        self._stations = stations or []
        self._counts = ClientCallCounts()
        self.kpi_batches: list[list[str]] = []
        self.kpi_plant_counts: list[int] = []

    def is_logged_in(self) -> bool:
        return True

    def call_counts(self) -> ClientCallCounts:
        return self._counts

    def set_kpi_plant_count(self, plant_count: int) -> None:
        self.kpi_plant_counts.append(plant_count)

    async def login(self) -> None:
        self._counts.login += 1

    async def list_stations(self) -> StationListResult:
        self._counts.station_list += 1
        return StationListResult(stations=self._stations, variant="direct_list", pages_retrieved=1)

    async def get_station_real_kpi(self, station_codes: list[str]) -> KpiBatchResult:
        self._counts.station_real_kpi += 1
        self.kpi_batches.append(list(station_codes))
        rows = [r for r in self._kpi_rows if r.get("stationCode") in station_codes]
        return KpiBatchResult(rows=rows, vendor_current_time_ms=self._vendor_ms)

    async def close(self) -> None:
        return None


def kpi_row(code: str, **items: Any) -> dict[str, Any]:
    return {"stationCode": code, "dataItemMap": items}


def real_adapter(client: ScriptedClient) -> FusionSolarAdapter:
    return FusionSolarAdapter(client, allow_synthetic_fields=False)


# --------------------------------------------------------------------- #
# performance-ratio normalization                                       #
# --------------------------------------------------------------------- #


def test_percent_style_pr_is_normalized():
    diag = KpiDiagnostics()
    assert normalize_performance_ratio(89, diag) == 0.89
    assert normalize_performance_ratio(100, diag) == 1.0
    assert diag.invalid_values == 0


def test_already_normalized_pr_is_accepted_as_compatibility():
    diag = KpiDiagnostics()
    assert normalize_performance_ratio(0.82, diag) == 0.82
    assert normalize_performance_ratio(0.0, diag) == 0.0
    assert normalize_performance_ratio(1.0, diag) == 1.0
    assert diag.invalid_values == 0


def test_a_json_integer_too_large_for_a_float_is_counted_not_raised():
    # JSON decoding yields a Python int of any size, but float() overflows on
    # a thousand-digit one. That is malformed data like NaN or "abc": counted
    # and dropped, never an OverflowError escaping run_cycle().
    diag = KpiDiagnostics()
    huge = int("1" * 1000)
    assert normalize_performance_ratio(huge, diag) is None
    assert _finite_float(huge, diag) is None
    assert diag.invalid_values == 2


def test_invalid_pr_values_are_rejected_and_counted():
    diag = KpiDiagnostics()
    assert normalize_performance_ratio(-0.5, diag) is None
    assert normalize_performance_ratio(250, diag) is None
    assert normalize_performance_ratio(float("nan"), diag) is None
    assert normalize_performance_ratio(float("inf"), diag) is None
    assert normalize_performance_ratio("abc", diag) is None
    assert diag.invalid_values == 5


# --------------------------------------------------------------------- #
# numeric validation & units                                            #
# --------------------------------------------------------------------- #


async def test_units_preserved_and_nan_inf_rejected():
    client = ScriptedClient(
        [
            kpi_row(
                "NE=1",
                day_power=123.5,
                total_power=float("inf"),
                performance_ratio=89,
                real_health_state=3,
            )
        ]
    )
    adapter = real_adapter(client)
    (reading,) = await adapter.fetch_plant_kpis(["NE=1"])
    assert reading.daily_energy_kwh == 123.5  # kWh stays kWh
    assert reading.total_energy_kwh is None  # inf rejected
    assert reading.performance_ratio == 0.89
    assert reading.status is PlantStatus.HEALTHY
    assert adapter.last_kpi_diagnostics.invalid_values == 1


async def test_real_adapter_never_derives_active_power():
    client = ScriptedClient([kpi_row("NE=1", real_power=500.0, day_power=1.0, real_health_state=3)])
    adapter = real_adapter(client)
    (reading,) = await adapter.fetch_plant_kpis(["NE=1"])
    # real_power is not a documented station real-KPI field: stays None.
    assert reading.active_power_kw is None


async def test_mock_adapter_maps_synthetic_active_power():
    client = ScriptedClient([kpi_row("NE=1", real_power=500.0, day_power=1.0, real_health_state=3)])
    adapter = FusionSolarAdapter(client, allow_synthetic_fields=True)
    (reading,) = await adapter.fetch_plant_kpis(["NE=1"])
    assert reading.active_power_kw == 500.0


async def test_unknown_health_state_maps_to_unknown():
    # A numeric code outside 1/2/3 is the documented "else unknown" case:
    # UNKNOWN, and NOT counted as invalid data.
    client = ScriptedClient([kpi_row("NE=1", real_health_state=9)])
    adapter = real_adapter(client)
    (reading,) = await adapter.fetch_plant_kpis(["NE=1"])
    assert reading.status is PlantStatus.UNKNOWN
    assert adapter.last_kpi_diagnostics.invalid_values == 0


async def test_integral_float_health_state_is_accepted():
    # 3.0 is the integer code 3 expressed as a JSON number — valid.
    client = ScriptedClient([kpi_row("NE=1", day_power=1.0, real_health_state=3.0)])
    adapter = real_adapter(client)
    (reading,) = await adapter.fetch_plant_kpis(["NE=1"])
    assert reading.status is PlantStatus.HEALTHY
    assert adapter.last_kpi_diagnostics.complete


async def test_absent_health_state_is_unknown_without_counting():
    client = ScriptedClient([kpi_row("NE=1", day_power=1.0)])
    adapter = real_adapter(client)
    (reading,) = await adapter.fetch_plant_kpis(["NE=1"])
    assert reading.status is PlantStatus.UNKNOWN
    assert adapter.last_kpi_diagnostics.complete


@pytest.mark.parametrize(
    "bad",
    [
        "broken",
        True,
        float("nan"),
        float("inf"),
        [3],
        # Fractional numbers must never be truncated into a valid code:
        # int(3.7) would read as HEALTHY and int(1.5) as DISCONNECTED.
        3.7,
        1.5,
        "3.7",
    ],
)
async def test_malformed_health_state_is_counted_as_invalid(bad: Any):
    # Present but unreadable is malformed data, and it is NOT persisted:
    # writing UNKNOWN over the plant's stored status would downgrade a
    # healthy plant on the strength of a field we could not read. An absent
    # field, or a code we simply do not map, still yields a reading — see
    # test_unknown_health_state_maps_to_unknown.
    client = ScriptedClient([kpi_row("NE=1", day_power=1.0, real_health_state=bad)])
    adapter = real_adapter(client)
    assert await adapter.fetch_plant_kpis(["NE=1"]) == []
    diag = adapter.last_kpi_diagnostics
    assert diag.invalid_values == 1
    assert not diag.complete


async def test_missing_pr_stays_none_without_invalid_count():
    client = ScriptedClient([kpi_row("NE=1", day_power=1.0, real_health_state=3)])
    adapter = real_adapter(client)
    (reading,) = await adapter.fetch_plant_kpis(["NE=1"])
    assert reading.performance_ratio is None
    assert adapter.last_kpi_diagnostics.invalid_values == 0


# --------------------------------------------------------------------- #
# timestamp provenance                                                  #
# --------------------------------------------------------------------- #


async def test_vendor_server_time_and_received_at_are_distinct():
    client = ScriptedClient([kpi_row("NE=1", day_power=1.0)])
    adapter = real_adapter(client)
    before = datetime.now(UTC)
    (reading,) = await adapter.fetch_plant_kpis(["NE=1"])
    after = datetime.now(UTC)
    assert reading.vendor_server_time == datetime.fromtimestamp(VENDOR_MS / 1000, tz=UTC)
    assert before <= reading.ts <= after  # ts is local received-at time


async def test_invalid_vendor_time_falls_back_to_none():
    client = ScriptedClient([kpi_row("NE=1", day_power=1.0)], vendor_ms=None)
    adapter = real_adapter(client)
    (reading,) = await adapter.fetch_plant_kpis(["NE=1"])
    assert reading.vendor_server_time is None
    assert reading.ts is not None


# --------------------------------------------------------------------- #
# batching & count diagnostics                                          #
# --------------------------------------------------------------------- #


async def test_batches_of_100_sequential():
    codes = [f"NE={i}" for i in range(250)]
    client = ScriptedClient([kpi_row(c, day_power=1.0) for c in codes])
    adapter = real_adapter(client)
    readings = await adapter.fetch_plant_kpis(codes)
    assert len(readings) == 250
    assert [len(b) for b in client.kpi_batches] == [100, 100, 50]
    # The FULL count is announced before the first batch (budget scaling).
    assert client.kpi_plant_counts == [250]
    diag = adapter.last_kpi_diagnostics
    assert diag.batches == 3 and diag.calls_consumed == 3 and diag.complete


async def test_missing_rows_are_counted_and_cycle_not_complete():
    client = ScriptedClient([kpi_row("NE=1", day_power=1.0)])
    adapter = real_adapter(client)
    readings = await adapter.fetch_plant_kpis(["NE=1", "NE=2", "NE=3"])
    assert len(readings) == 1
    diag = adapter.last_kpi_diagnostics
    assert diag.requested == 3 and diag.returned == 1 and diag.missing == 2
    assert not diag.complete


async def test_duplicate_rows_are_counted_once():
    client = ScriptedClient([kpi_row("NE=1", day_power=1.0), kpi_row("NE=1", day_power=2.0)])
    adapter = real_adapter(client)
    readings = await adapter.fetch_plant_kpis(["NE=1"])
    assert len(readings) == 1
    assert adapter.last_kpi_diagnostics.duplicates == 1
    assert not adapter.last_kpi_diagnostics.complete


async def test_a_valid_duplicate_replaces_an_unreadable_first_copy():
    # An unreadable row is a row the adapter does NOT have — it is counted
    # invalid and never persisted. Marking the station "seen" before that
    # verdict let the unreadable copy claim the slot, so a second, perfectly
    # good copy hit the duplicate branch and was dropped: a usable reading
    # lost and the stale stored value kept, for a station the vendor did
    # report correctly.
    rows = [
        {"stationCode": "NE=1", "dataItemMap": "not-a-map"},
        kpi_row("NE=1", day_power=7.5),
    ]
    adapter = real_adapter(ScriptedClient(rows))
    readings = await adapter.fetch_plant_kpis(["NE=1"])

    assert [(r.vendor_plant_id, r.daily_energy_kwh) for r in readings] == [("NE=1", 7.5)]
    diag = adapter.last_kpi_diagnostics
    assert diag.invalid_values == 1  # the unreadable copy is still counted
    assert diag.duplicates == 0  # the good copy was the first one ACCEPTED
    assert diag.missing == 0  # the station was answered, not absent


async def test_a_clean_copy_replaces_a_partially_unreadable_one():
    # A row with an unreadable FIELD is only partly a row we have. Marking it
    # accepted closed the station to further copies, so a later copy carrying
    # a valid 7.5 kWh was discarded as a duplicate and the None was persisted
    # as the latest value — the same good-data loss the answered/accepted
    # split was added to prevent, one level down.
    rows = [kpi_row("NE=1", day_power="bad"), kpi_row("NE=1", day_power=7.5)]
    adapter = real_adapter(ScriptedClient(rows))
    readings = await adapter.fetch_plant_kpis(["NE=1"])

    assert [(r.vendor_plant_id, r.daily_energy_kwh) for r in readings] == [("NE=1", 7.5)]
    diag = adapter.last_kpi_diagnostics
    assert diag.returned == 1  # REPLACED, never appended beside the partial one
    assert diag.invalid_values == 1  # the unreadable field is still counted
    assert diag.duplicates == 0  # an upgrade, not an extra
    assert diag.missing == 0


async def test_a_partial_copy_never_downgrades_a_clean_one():
    # The replacement runs one way only: once every field of a copy has read,
    # a later copy with an unreadable field is an extra, not a correction.
    rows = [kpi_row("NE=1", day_power=7.5), kpi_row("NE=1", day_power="bad")]
    adapter = real_adapter(ScriptedClient(rows))
    readings = await adapter.fetch_plant_kpis(["NE=1"])

    assert [r.daily_energy_kwh for r in readings] == [7.5]
    diag = adapter.last_kpi_diagnostics
    assert diag.duplicates == 1
    assert diag.invalid_values == 0  # the second copy was never parsed


async def test_a_partial_reading_is_still_kept_when_no_better_copy_arrives():
    # One bad field must not cost the good fields beside it: the row is kept,
    # the bad value dropped and counted, and the station is NOT reported
    # missing. This is the pre-existing contract and the replacement above
    # must not quietly turn it into a dropped row.
    rows = [kpi_row("NE=1", day_power=123.5, total_power=float("inf"))]
    adapter = real_adapter(ScriptedClient(rows))
    (reading,) = await adapter.fetch_plant_kpis(["NE=1"])

    assert reading.daily_energy_kwh == 123.5
    assert reading.total_energy_kwh is None
    diag = adapter.last_kpi_diagnostics
    assert (diag.returned, diag.missing, diag.invalid_values) == (1, 0, 1)


async def test_a_duplicate_of_an_accepted_row_is_still_a_duplicate():
    # The relaxation above must not swallow real duplicates: once a reading
    # is in hand, a further copy is an extra, not a second chance.
    rows = [kpi_row("NE=1", day_power=1.0), kpi_row("NE=1", day_power=2.0)]
    adapter = real_adapter(ScriptedClient(rows))
    readings = await adapter.fetch_plant_kpis(["NE=1"])
    assert [r.daily_energy_kwh for r in readings] == [1.0]  # the first wins
    assert adapter.last_kpi_diagnostics.duplicates == 1


async def test_unexpected_rows_are_excluded_and_counted():
    client = ScriptedClient([kpi_row("NE=1", day_power=1.0), kpi_row("NE=999", day_power=9.0)])
    # ScriptedClient filters by requested codes; bypass to simulate a rogue row.
    client._kpi_rows.append(kpi_row("NE=other", day_power=3.0))

    async def rogue(codes: list[str]) -> KpiBatchResult:
        client._counts.station_real_kpi += 1
        return KpiBatchResult(
            rows=[kpi_row("NE=1", day_power=1.0), kpi_row("NE=rogue", day_power=3.0)],
            vendor_current_time_ms=VENDOR_MS,
        )

    client.get_station_real_kpi = rogue  # type: ignore[method-assign]
    adapter = real_adapter(client)
    readings = await adapter.fetch_plant_kpis(["NE=1"])
    assert [r.vendor_plant_id for r in readings] == ["NE=1"]
    assert adapter.last_kpi_diagnostics.unexpected == 1


async def test_rows_are_validated_against_their_own_batch():
    """A row belonging to a later batch must not be accepted by an earlier one.

    Otherwise the misrouted (possibly stale) value wins and the station's
    real row, arriving in its own batch, is discarded as a duplicate.
    """

    class CrossBatchClient(ScriptedClient):
        def __init__(self) -> None:
            super().__init__([])
            self.calls = 0

        async def get_station_real_kpi(self, station_codes: list[str]) -> KpiBatchResult:
            self._counts.station_real_kpi += 1
            self.calls += 1
            value = 999.0 if self.calls == 1 else 1.0
            # Batch 1 (NE=0..99) wrongly carries NE=100, which is batch 2's.
            return KpiBatchResult(
                rows=[kpi_row("NE=100", day_power=value)], vendor_current_time_ms=VENDOR_MS
            )

    client = CrossBatchClient()
    adapter = real_adapter(client)
    readings = await adapter.fetch_plant_kpis([f"NE={i}" for i in range(150)])

    assert len(readings) == 1
    assert readings[0].daily_energy_kwh == 1.0  # the row from its OWN batch won
    diag = adapter.last_kpi_diagnostics
    assert diag.unexpected == 1  # the misrouted row was rejected and counted
    assert diag.duplicates == 0  # the correct row was never treated as a dup


async def test_an_unreadable_capacity_is_counted_and_not_written():
    # It used to go into a throwaway diagnostics object: the refresh looked
    # clean while the station's capacity silently became None.
    class Client:
        async def login(self) -> None: ...

        def is_logged_in(self) -> bool:
            return True

        def set_kpi_plant_count(self, plant_count: int) -> None: ...

        def call_counts(self) -> ClientCallCounts:
            return ClientCallCounts()

        async def list_stations(self) -> StationListResult:
            return StationListResult(
                stations=[
                    {"stationCode": "NE=1", "stationName": "A", "capacity": 2.5},
                    {"stationCode": "NE=2", "stationName": "B", "capacity": float("inf")},
                ],
                variant="paginated",
                pages_retrieved=1,
            )

        async def get_station_real_kpi(self, station_codes: list[str]) -> KpiBatchResult:
            raise AssertionError("not used")

        async def close(self) -> None: ...

    adapter = FusionSolarAdapter(Client())
    plants = await adapter.list_plants()
    assert [p.capacity_kwp for p in plants] == [2500.0, None]
    assert adapter.last_inventory_diagnostics.invalid_capacity == 1


async def test_a_capacity_that_overflows_the_unit_conversion_is_counted():
    # 1e308 MW passes the first check and becomes infinity in kWp. An
    # infinite capacity is not None, so it would be WRITTEN over a good
    # stored value — what gets stored is what has to be validated.
    class Client:
        async def login(self) -> None: ...

        def is_logged_in(self) -> bool:
            return True

        def set_kpi_plant_count(self, plant_count: int) -> None: ...

        def call_counts(self) -> ClientCallCounts:
            return ClientCallCounts()

        async def list_stations(self) -> StationListResult:
            return StationListResult(
                stations=[{"stationCode": "NE=1", "stationName": "A", "capacity": 1e308}],
                variant="paginated",
                pages_retrieved=1,
            )

        async def get_station_real_kpi(self, station_codes: list[str]) -> KpiBatchResult:
            raise AssertionError("not used")

        async def close(self) -> None: ...

    adapter = FusionSolarAdapter(Client())
    (plant,) = await adapter.list_plants()
    assert plant.capacity_kwp is None
    assert adapter.last_inventory_diagnostics.invalid_capacity == 1


@pytest.mark.parametrize("bad", ["3", "healthy", [3], {"code": 3}])
def test_a_health_state_that_is_not_a_number_is_counted(bad: Any):
    # int("3") would map a STRING to HEALTHY and count nothing — the
    # confident-but-unearned reading the strict reader exists to prevent.
    diag = KpiDiagnostics()
    assert _health_status(bad, diag) is PlantStatus.UNKNOWN
    assert diag.invalid_values == 1


async def test_a_failed_batch_still_publishes_what_it_spent():
    # A batch that raises has already spent its calls. Keeping the previous
    # fetch's diagnostics would show the scheduler a complete result that
    # never happened and hide KPI budget already consumed.
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        async def login(self) -> None: ...

        def is_logged_in(self) -> bool:
            return True

        def set_kpi_plant_count(self, plant_count: int) -> None: ...

        def call_counts(self) -> ClientCallCounts:
            return ClientCallCounts(station_real_kpi=self.calls)

        async def list_stations(self) -> StationListResult:
            raise AssertionError("not used")

        async def get_station_real_kpi(self, station_codes: list[str]) -> KpiBatchResult:
            self.calls += 1
            if self.calls >= 2:
                raise AdapterTransientError("vendor failed the second batch")
            return KpiBatchResult(
                rows=[{"stationCode": c, "dataItemMap": {"day_power": 1.0}} for c in station_codes],
                vendor_current_time_ms=VENDOR_MS,
            )

    adapter = FusionSolarAdapter(Client())
    await adapter.fetch_plant_kpis([f"NE={i}" for i in range(100)])  # a clean fetch first
    assert adapter.last_kpi_diagnostics.complete

    with pytest.raises(AdapterTransientError):
        await adapter.fetch_plant_kpis([f"NE={i}" for i in range(150)])
    diag = adapter.last_kpi_diagnostics
    assert diag.requested == 150 and diag.returned == 0  # the FAILED attempt
    assert diag.calls_consumed == 1  # and the call it spent is visible


async def test_an_unreadable_data_map_never_becomes_a_reading():
    # Substituting {} builds a reading with every KPI unset and status
    # UNKNOWN, which the repository writes over a healthy stored status:
    # one malformed row silently downgrading a plant.
    class Client:
        async def login(self) -> None: ...

        def is_logged_in(self) -> bool:
            return True

        def set_kpi_plant_count(self, plant_count: int) -> None: ...

        def call_counts(self) -> ClientCallCounts:
            return ClientCallCounts()

        async def list_stations(self) -> StationListResult:
            raise AssertionError("not used")

        async def get_station_real_kpi(self, station_codes: list[str]) -> KpiBatchResult:
            return KpiBatchResult(
                rows=[
                    {"stationCode": "NE=1", "dataItemMap": "not-an-object"},
                    {"stationCode": "NE=2", "dataItemMap": ["not", "an", "object"]},
                    {"stationCode": "NE=3", "dataItemMap": {"day_power": 4.0}},
                ],
                vendor_current_time_ms=VENDOR_MS,
            )

        async def close(self) -> None: ...

    adapter = FusionSolarAdapter(Client())
    readings = await adapter.fetch_plant_kpis(["NE=1", "NE=2", "NE=3"])
    assert [r.vendor_plant_id for r in readings] == ["NE=3"]  # only the readable row
    diag = adapter.last_kpi_diagnostics
    assert diag.invalid_values == 2
    # Counted as unreadable rather than missing: the vendor DID answer for
    # them, with something unusable. Either way invalid_values makes the
    # cycle incomplete, and requested=3 vs returned=1 shows the gap.
    assert (diag.requested, diag.returned, diag.missing) == (3, 1, 0)


async def test_an_unreadable_health_state_never_downgrades_a_stored_status():
    # The persistence side of the strict health reader: a plant whose stored
    # status is HEALTHY must not become UNKNOWN because one field arrived as
    # text. A code we simply do not map is different — see below.
    repo = InMemoryRepository()
    await repo.upsert_plants([PlantInfo(vendor="fusionsolar", vendor_plant_id="NE=1", name="P")])
    await repo.record_kpis(
        [
            PlantKpiReading(
                vendor="fusionsolar",
                vendor_plant_id="NE=1",
                ts=datetime.now(UTC),
                status=PlantStatus.HEALTHY,
            )
        ]
    )
    assert (await repo.list_plants())[0].status is PlantStatus.HEALTHY

    client = ScriptedClient([kpi_row("NE=1", day_power=1.0, real_health_state="3")])
    adapter = real_adapter(client)
    readings = await adapter.fetch_plant_kpis(["NE=1"])
    await repo.record_kpis(readings)
    assert (await repo.list_plants())[0].status is PlantStatus.HEALTHY
    assert adapter.last_kpi_diagnostics.invalid_values == 1


async def test_an_unmapped_health_code_still_yields_a_reading():
    # "else unknown" is the documented mapping, not a malformed field: a
    # code we do not map is read successfully and persisted as UNKNOWN.
    client = ScriptedClient([kpi_row("NE=1", day_power=1.0, real_health_state=9)])
    adapter = real_adapter(client)
    (reading,) = await adapter.fetch_plant_kpis(["NE=1"])
    assert reading.status is PlantStatus.UNKNOWN
    assert reading.daily_energy_kwh == 1.0
    assert adapter.last_kpi_diagnostics.invalid_values == 0
