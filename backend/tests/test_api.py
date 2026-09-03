from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.adapters.fusionsolar.adapter import FusionSolarAdapter
from app.adapters.fusionsolar.mock_client import MockFusionSolarClient
from app.config import Settings
from app.main import create_app
from app.scheduler.ingestion import IngestionScheduler
from tests.conftest import FIXED_NOON_UTC


@pytest.fixture
async def client():
    settings = Settings(_env_file=None)  # defaults: mock mode, in-memory, no scheduler
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        # Seed through the real ingestion path (one scheduler cycle).
        adapter = FusionSolarAdapter(
            MockFusionSolarClient(now=lambda: FIXED_NOON_UTC), allow_synthetic_fields=True
        )
        result = await IngestionScheduler(adapter, app.state.repository).run_cycle()
        assert result.error is None
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test/api/v1"
        ) as http_client:
            yield http_client


async def test_health(client: httpx.AsyncClient):
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["fusionsolar_mode"] == "mock"
    assert body["scheduler_enabled"] is False


async def test_list_plants_with_latest_kpis(client: httpx.AsyncClient):
    response = await client.get("/plants")
    assert response.status_code == 200
    plants = response.json()
    assert len(plants) == 3
    for plant in plants:
        assert plant["vendor"] == "fusionsolar"
        assert plant["latest_kpi"] is not None
        assert plant["latest_kpi"]["daily_energy_kwh"] > 0
    statuses = {p["vendor_plant_id"]: p["status"] for p in plants}
    assert statuses["NE=MOCK001"] == "healthy"
    assert statuses["NE=MOCK003"] == "faulty"


async def test_get_single_plant(client: httpx.AsyncClient):
    plants = (await client.get("/plants")).json()
    plant_id = plants[0]["id"]
    response = await client.get(f"/plants/{plant_id}")
    assert response.status_code == 200
    assert response.json()["id"] == plant_id


async def test_unknown_plant_is_404(client: httpx.AsyncClient):
    assert (await client.get("/plants/9999")).status_code == 404
    assert (await client.get("/plants/9999/kpis/latest")).status_code == 404


async def test_latest_kpi_endpoint(client: httpx.AsyncClient):
    plant_id = (await client.get("/plants")).json()[0]["id"]
    response = await client.get(f"/plants/{plant_id}/kpis/latest")
    assert response.status_code == 200
    body = response.json()
    assert body["active_power_kw"] > 0
    assert body["ts"] is not None


async def test_kpi_history_window(client: httpx.AsyncClient):
    plant_id = (await client.get("/plants")).json()[0]["id"]
    # Readings are stamped with ingestion time, so anchor the window on now.
    now = datetime.now(UTC)
    start = (now - timedelta(hours=1)).isoformat()
    end = (now + timedelta(hours=1)).isoformat()
    response = await client.get(f"/plants/{plant_id}/kpis", params={"start": start, "end": end})
    assert response.status_code == 200
    assert len(response.json()) == 1


async def test_kpi_history_rejects_inverted_window(client: httpx.AsyncClient):
    plant_id = (await client.get("/plants")).json()[0]["id"]
    response = await client.get(
        f"/plants/{plant_id}/kpis",
        params={"start": "2026-09-02T00:00:00Z", "end": "2026-09-01T00:00:00Z"},
    )
    assert response.status_code == 422
