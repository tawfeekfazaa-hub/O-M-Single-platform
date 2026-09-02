"""The mock adapter is the development target — its behaviour is contract.

The mock KPI rows carry SYNTHETIC mock-only fields (real_power,
performance_ratio) that the documented real contract does not provide;
they are asserted here deliberately so the MVP dashboard keeps rendering
plausible values (see mock_client docstring).
"""

from __future__ import annotations

from app.adapters.base import PlantStatus
from app.adapters.fusionsolar.adapter import FusionSolarAdapter
from app.adapters.fusionsolar.mock_client import MockFusionSolarClient
from tests.conftest import FIXED_NIGHT_UTC, FIXED_NOON_UTC


async def test_list_plants_normalizes_vendor_payload(adapter: FusionSolarAdapter):
    plants = await adapter.list_plants()
    assert len(plants) == 3
    by_id = {p.vendor_plant_id: p for p in plants}
    riyadh = by_id["NE=MOCK001"]
    assert riyadh.vendor == "fusionsolar"
    assert riyadh.name == "AQ Riyadh Solar Park 1"
    # Vendor reports MW; we store kWp.
    assert riyadh.capacity_kwp == 2500.0
    assert riyadh.address == "Riyadh, Saudi Arabia"
    diag = adapter.last_inventory_diagnostics
    assert diag.stations == 3 and diag.variant == "direct_list" and diag.pages_retrieved == 1


async def test_kpis_at_noon_are_plausible(adapter: FusionSolarAdapter):
    plants = await adapter.list_plants()
    readings = await adapter.fetch_plant_kpis([p.vendor_plant_id for p in plants])
    assert len(readings) == 3
    by_id = {r.vendor_plant_id: r for r in readings}

    healthy = by_id["NE=MOCK001"]
    assert healthy.status is PlantStatus.HEALTHY
    # Synthetic mock-only active power keeps the dashboard functional.
    assert healthy.active_power_kw is not None
    assert 0 < healthy.active_power_kw <= 2500.0
    assert healthy.daily_energy_kwh is not None and healthy.daily_energy_kwh > 0
    assert healthy.total_energy_kwh is not None
    assert healthy.total_energy_kwh > healthy.daily_energy_kwh
    assert healthy.performance_ratio is not None
    assert 0.7 <= healthy.performance_ratio <= 0.9
    assert healthy.ts.tzinfo is not None
    assert healthy.vendor_server_time is not None  # mock envelope currentTime

    faulty = by_id["NE=MOCK003"]
    assert faulty.status is PlantStatus.FAULTY
    assert faulty.active_power_kw is not None and faulty.active_power_kw < 1200.0 * 0.5

    diag = adapter.last_kpi_diagnostics
    assert diag.requested == 3 and diag.returned == 3 and diag.missing == 0
    assert diag.complete


async def test_no_production_at_night():
    client = MockFusionSolarClient(now=lambda: FIXED_NIGHT_UTC)
    adapter = FusionSolarAdapter(client, allow_synthetic_fields=True)
    readings = await adapter.fetch_plant_kpis(["NE=MOCK001"])
    (reading,) = readings
    assert reading.active_power_kw == 0.0
    # After sunset the full daily yield has accumulated.
    assert reading.daily_energy_kwh is not None
    assert reading.daily_energy_kwh > 2500.0 * 5.0


async def test_mock_is_deterministic():
    a = MockFusionSolarClient(now=lambda: FIXED_NOON_UTC)
    b = MockFusionSolarClient(now=lambda: FIXED_NOON_UTC)
    result_a = await a.get_station_real_kpi(["NE=MOCK001", "NE=MOCK002"])
    result_b = await b.get_station_real_kpi(["NE=MOCK001", "NE=MOCK002"])
    assert result_a.rows == result_b.rows
    assert (await a.list_stations()).stations == (await b.list_stations()).stations


async def test_fetch_only_requested_plants(adapter: FusionSolarAdapter):
    readings = await adapter.fetch_plant_kpis(["NE=MOCK002"])
    assert [r.vendor_plant_id for r in readings] == ["NE=MOCK002"]


async def test_mock_supports_paginated_variant_for_tests():
    client = MockFusionSolarClient(
        now=lambda: FIXED_NOON_UTC, station_list_variant="paginated", page_size=2
    )
    result = await client.list_stations()
    assert result.variant == "paginated"
    assert result.pages_retrieved == 2  # 3 stations / page_size 2
    assert len(result.stations) == 3


async def test_health_check_consumes_no_calls(mock_client: MockFusionSolarClient):
    adapter = FusionSolarAdapter(mock_client, allow_synthetic_fields=True)
    before = mock_client.call_counts().as_dict()
    assert await adapter.health_check() is True
    assert mock_client.call_counts().as_dict() == before
