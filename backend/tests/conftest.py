from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.adapters.fusionsolar.adapter import FusionSolarAdapter
from app.adapters.fusionsolar.mock_client import MockFusionSolarClient
from app.repositories.memory import InMemoryRepository

# 09:00 UTC = 12:00 in Riyadh (UTC+3) -> exact solar noon in the mock model.
FIXED_NOON_UTC = datetime(2026, 9, 1, 9, 0, 0, tzinfo=UTC)
# 19:00 UTC = 22:00 local -> after sunset.
FIXED_NIGHT_UTC = datetime(2026, 9, 1, 19, 0, 0, tzinfo=UTC)


@pytest.fixture
def mock_client() -> MockFusionSolarClient:
    return MockFusionSolarClient(now=lambda: FIXED_NOON_UTC)


@pytest.fixture
def adapter(mock_client: MockFusionSolarClient) -> FusionSolarAdapter:
    # Mirrors the factory: only the mock adapter maps synthetic fields.
    return FusionSolarAdapter(mock_client, allow_synthetic_fields=True)


@pytest.fixture
def repository() -> InMemoryRepository:
    return InMemoryRepository()
