"""Adapter-level normalization tests: units, finite validation, PR
normalization, health mapping, timestamp provenance, count diagnostics,
and the synthetic-fields boundary between mock and real mapping."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.adapters.base import PlantStatus
from app.adapters.fusionsolar.adapter import (
    FusionSolarAdapter,
    KpiDiagnostics,
    normalize_performance_ratio,
)
from app.adapters.fusionsolar.client import ClientCallCounts, KpiBatchResult, StationListResult

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
    client = ScriptedClient([kpi_row("NE=1", real_health_state=9)])
    adapter = real_adapter(client)
    (reading,) = await adapter.fetch_plant_kpis(["NE=1"])
    assert reading.status is PlantStatus.UNKNOWN


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
