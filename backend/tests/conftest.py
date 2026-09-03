from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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


# --------------------------------------------------------------------- #
# Live-database fixtures (tests marked `dbtest`)                        #
# --------------------------------------------------------------------- #
# These run ONLY against a real PostgreSQL/TimescaleDB named by
# TEST_DATABASE_URL. The offline suite deselects them (`-m "not dbtest"`), so
# the default `pytest` run stays credential-free, network-free and skip-free.
# Each test gets its OWN database: migrations are DDL, and a shared schema
# would make one test's rollback another test's missing table.


def _admin_url(url: URL) -> URL:
    """The supplied URL, used as-is for CREATE/DROP DATABASE.

    CREATE DATABASE only needs a connection to some database other than the one
    being created, and the generated names are unique, so the supplied one
    always qualifies. Substituting ``postgres`` here used to be a silent extra
    requirement: a role that can reach the database it was given but not the
    cluster's ``postgres`` failed every live test before the first one ran —
    and it contradicted the documented promise that the named database is only
    a connection point.
    """
    return url


@pytest.fixture(scope="session")
def test_database_url() -> URL:
    raw = os.environ.get("TEST_DATABASE_URL")
    if not raw:
        pytest.skip("TEST_DATABASE_URL is not set — live-database tests need one")
    url = make_url(raw)
    if url.get_backend_name() != "postgresql":
        pytest.skip(f"TEST_DATABASE_URL is not a PostgreSQL URL: {url.get_backend_name()}")
    return url


@pytest.fixture
async def db_url(test_database_url: URL) -> AsyncIterator[URL]:
    """A freshly created, uniquely named database, dropped afterwards."""
    name = f"aq_dbtest_{uuid.uuid4().hex[:16]}"
    admin = create_async_engine(_admin_url(test_database_url), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
        yield test_database_url.set(database=name)
    finally:
        async with admin.connect() as conn:
            # Terminate stragglers first: an engine that has not finished
            # disposing would otherwise make DROP DATABASE fail and leak.
            await conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": name},
            )
            await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        await admin.dispose()


@pytest.fixture
async def db_engine(db_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()
