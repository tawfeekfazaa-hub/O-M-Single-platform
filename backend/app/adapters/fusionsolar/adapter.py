"""FusionSolar adapter: maps vendor payloads to the normalized model.

Mapping rules (docs/FUSIONSOLAR-CONTRACT.md):
- station ``capacity`` is reported in MW and stored as kWp;
- ``day_power``/``total_power`` are kWh and stay kWh;
- ``performance_ratio`` is tenant/version-dependent: percent-style values
  (1 < v <= 100) normalize to 0..1, already-normalized 0..1 values are an
  explicitly tested compatibility case, anything else is rejected (None);
- NaN/infinity are rejected for every numeric field;
- ``real_health_state``: 1 disconnected, 2 faulty, 3 healthy, else unknown;
- active power: the documented getStationRealKpi contract exposes NO
  station-level active-power field, so the REAL adapter stores None. Only
  the mock adapter (allow_synthetic_fields=True) maps the synthetic
  ``real_power`` field so the MVP dashboard has data to render;
- ``params.currentTime`` is carried on the in-flight reading as the vendor
  SERVER time (never a device measurement timestamp) next to the local
  received-at time; the persisted KPI schema does not store it yet —
  durable retention (with the full raw envelope) arrives with the PR-2
  Raw/Quarantine layer.

Diagnostics carry counts only — never station identifiers or values.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar

from app.adapters.base import (
    AdapterError,
    PlantInfo,
    PlantKpiReading,
    PlantStatus,
    VendorAdapter,
)
from app.adapters.fusionsolar.client import FusionSolarClient
from app.adapters.fusionsolar.policy import KPI_BATCH_SIZE

_HEALTH_TO_STATUS = {
    1: PlantStatus.DISCONNECTED,
    2: PlantStatus.FAULTY,
    3: PlantStatus.HEALTHY,
}


@dataclass(slots=True)
class InventoryDiagnostics:
    """Counts-only summary of the last station-list retrieval."""

    stations: int = 0
    pages_retrieved: int = 0
    variant: str = ""
    duplicates_removed: int = 0
    calls_consumed: int = 0
    # A failed retrieval still SPENT its calls. The scheduler needs that to
    # know when the budget can carry the next attempt; without it a refresh
    # that died on page 3 is retried while its own three calls are still
    # occupying slots.
    failed: bool = False


@dataclass(slots=True)
class KpiDiagnostics:
    """Counts-only summary of the last KPI fetch (no identifiers/values)."""

    requested: int = 0
    returned: int = 0
    missing: int = 0
    duplicates: int = 0
    unexpected: int = 0
    invalid_values: int = 0
    batches: int = 0
    calls_consumed: int = 0

    @property
    def complete(self) -> bool:
        return (
            self.missing == 0
            and self.duplicates == 0
            and self.unexpected == 0
            and self.invalid_values == 0
        )


def _finite_float(value: Any, diagnostics: KpiDiagnostics) -> float | None:
    """Best-effort float that rejects NaN/inf/garbage (counted, not stored)."""
    if value is None:
        return None
    if isinstance(value, bool):
        diagnostics.invalid_values += 1
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        diagnostics.invalid_values += 1
        return None
    if not math.isfinite(number):
        diagnostics.invalid_values += 1
        return None
    return number


def normalize_performance_ratio(value: Any, diagnostics: KpiDiagnostics) -> float | None:
    """Normalize a tenant-dependent PR to the internal 0..1 contract."""
    number = _finite_float(value, diagnostics)
    if number is None:
        return None
    if 0.0 <= number <= 1.0:
        return number  # already-normalized compatibility form
    if 1.0 < number <= 100.0:
        return number / 100.0  # documented percent-style form (e.g. 89 -> 0.89)
    diagnostics.invalid_values += 1  # negative or impossible (> 100)
    return None


def _as_int(value: Any) -> int | None:
    """Strict integer read: never TRUNCATES a fractional number.

    ``int(3.7)`` would silently become the healthy code 3 and ``int(1.5)``
    the disconnected code 1, turning an unreadable status into a confidently
    mapped one. A non-integral number is not an integer code — it is
    malformed input, and the caller counts it as such.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number


def _health_status(value: Any, diagnostics: KpiDiagnostics) -> PlantStatus:
    """Map real_health_state, distinguishing ABSENT from UNPARSEABLE.

    An absent field and a documented-but-unmapped code both yield UNKNOWN
    without counting: the contract itself says "else unknown". A value that
    is present but cannot be read as an integer (bool, text, NaN) is
    malformed data and must be counted, or the scheduler would report the
    response as a complete success and persist the bad status.
    """
    if value is None:
        return PlantStatus.UNKNOWN
    code = _as_int(value)
    if code is None:
        diagnostics.invalid_values += 1
        return PlantStatus.UNKNOWN
    return _HEALTH_TO_STATUS.get(code, PlantStatus.UNKNOWN)


class FusionSolarAdapter(VendorAdapter):
    vendor: ClassVar[str] = "fusionsolar"

    def __init__(self, client: FusionSolarClient, *, allow_synthetic_fields: bool = False) -> None:
        self._client = client
        # True ONLY for the mock client: permits mapping the synthetic
        # mock-only fields (real_power, see mock_client docstring).
        self._allow_synthetic_fields = allow_synthetic_fields
        self.last_inventory_diagnostics = InventoryDiagnostics()
        self.last_kpi_diagnostics = KpiDiagnostics()

    async def authenticate(self) -> None:
        if not self._client.is_logged_in():
            await self._client.login()

    async def list_plants(self) -> list[PlantInfo]:
        before = self._client.call_counts().station_list
        try:
            result = await self._client.list_stations()
        except AdapterError:
            # Transport attempts, which is an UPPER bound on the budget
            # slots spent (the post-305 retry reuses its slot). Over-
            # reserving delays the next attempt slightly; under-reserving
            # would send it into a budget that cannot carry it.
            self.last_inventory_diagnostics = InventoryDiagnostics(
                calls_consumed=self._client.call_counts().station_list - before,
                failed=True,
            )
            raise
        plants: list[PlantInfo] = []
        throwaway = KpiDiagnostics()  # numeric validation counter for capacity
        for station in result.stations:
            code = station.get("stationCode")
            if not code:
                continue  # client already rejects these; belt and braces
            capacity_mw = _finite_float(station.get("capacity"), throwaway)
            plants.append(
                PlantInfo(
                    vendor=self.vendor,
                    vendor_plant_id=str(code),
                    name=str(station.get("stationName") or code),
                    # FusionSolar reports capacity in MW; we store kWp.
                    capacity_kwp=capacity_mw * 1000.0 if capacity_mw is not None else None,
                    address=station.get("stationAddr"),
                )
            )
        self.last_inventory_diagnostics = InventoryDiagnostics(
            stations=len(plants),
            pages_retrieved=result.pages_retrieved,
            variant=result.variant,
            duplicates_removed=result.duplicates_removed,
            calls_consumed=self._client.call_counts().station_list - before,
        )
        return plants

    async def fetch_plant_kpis(self, vendor_plant_ids: list[str]) -> list[PlantKpiReading]:
        diagnostics = KpiDiagnostics(requested=len(vendor_plant_ids))
        # Global set — used ONLY for the final "missing" diagnostic; each
        # row is validated against its own batch (see the loop below).
        requested = set(vendor_plant_ids)
        seen: set[str] = set()
        readings: list[PlantKpiReading] = []
        before = self._client.call_counts().station_real_kpi

        # The official KPI allowance is ceil(plants/100) per window, so the
        # client's budget must be scaled from the FULL requested count BEFORE
        # the first batch — otherwise every batch after the first would be
        # rejected by the 1-call constructor default.
        self._client.set_kpi_plant_count(len(vendor_plant_ids))

        # Sequential batches of at most 100 codes — never concurrent.
        for start in range(0, len(vendor_plant_ids), KPI_BATCH_SIZE):
            batch = vendor_plant_ids[start : start + KPI_BATCH_SIZE]
            # Membership is checked against THIS batch, not the whole
            # request: a row for a station belonging to a later batch is
            # misrouted data. Accepting it would let a stale value win and
            # would make the station's real row look like a duplicate.
            batch_codes = set(batch)
            result = await self._client.get_station_real_kpi(batch)
            diagnostics.batches += 1
            received_at = datetime.now(UTC)
            vendor_time = (
                datetime.fromtimestamp(result.vendor_current_time_ms / 1000.0, tz=UTC)
                if result.vendor_current_time_ms is not None
                else None
            )
            for row in result.rows:
                code = row.get("stationCode")
                if not code:
                    diagnostics.invalid_values += 1
                    continue
                code = str(code)
                if code not in batch_codes:
                    diagnostics.unexpected += 1
                    continue
                if code in seen:
                    diagnostics.duplicates += 1
                    continue
                seen.add(code)
                item = row.get("dataItemMap")
                if not isinstance(item, dict):
                    diagnostics.invalid_values += 1
                    item = {}

                if self._allow_synthetic_fields:
                    # SYNTHETIC mock-only field; see mock_client docstring.
                    active_power = _finite_float(item.get("real_power"), diagnostics)
                else:
                    # The documented station real-KPI contract exposes no
                    # active-power field — never derived, stays None.
                    active_power = None

                readings.append(
                    PlantKpiReading(
                        vendor=self.vendor,
                        vendor_plant_id=code,
                        ts=received_at,
                        active_power_kw=active_power,
                        daily_energy_kwh=_finite_float(item.get("day_power"), diagnostics),
                        total_energy_kwh=_finite_float(item.get("total_power"), diagnostics),
                        performance_ratio=normalize_performance_ratio(
                            item.get("performance_ratio"), diagnostics
                        )
                        if item.get("performance_ratio") is not None
                        else None,
                        status=_health_status(item.get("real_health_state"), diagnostics),
                        vendor_server_time=vendor_time,
                    )
                )

        diagnostics.returned = len(readings)
        diagnostics.missing = len(requested - seen)
        diagnostics.calls_consumed = self._client.call_counts().station_real_kpi - before
        self.last_kpi_diagnostics = diagnostics
        return readings

    async def health_check(self) -> bool:
        # Must not consume vendor rate budget: session presence only.
        return self._client.is_logged_in()

    async def close(self) -> None:
        await self._client.close()
