"""The real migrations, and the TimescaleDB constraints PR-2A1 must design around.

Everything here needs a live TimescaleDB — the pinned image from
docker-compose.yml, which is also what the `backend-db` CI job runs. Plain
PostgreSQL is not enough: the point of several of these tests is precisely what
TimescaleDB does and does not permit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.adapters.base import PlantInfo, PlantKpiReading, PlantStatus
from app.db.migrations import apply_pending, downgrade_to
from app.repositories.postgres import PostgresRepository

pytestmark = pytest.mark.dbtest

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

T0 = datetime(2026, 9, 1, 9, 0, 0, tzinfo=UTC)


@pytest.fixture
async def migrated(db_engine: AsyncEngine) -> AsyncEngine:
    await apply_pending(db_engine, MIGRATIONS_DIR, emit=lambda _: None)
    return db_engine


async def relation_exists(engine: AsyncEngine, name: str) -> bool:
    async with engine.connect() as conn:
        return bool(await conn.scalar(sa.text("SELECT to_regclass(:n) IS NOT NULL"), {"n": name}))


async def is_hypertable(engine: AsyncEngine, name: str) -> bool:
    async with engine.connect() as conn:
        return bool(
            await conn.scalar(
                sa.text(
                    "SELECT count(*) FROM timescaledb_information.hypertables "
                    "WHERE hypertable_name = :n"
                ),
                {"n": name},
            )
        )


# --------------------------------------------------------------------- #
# the real migrations, end to end                                       #
# --------------------------------------------------------------------- #


async def test_migrations_apply_from_an_empty_database(migrated: AsyncEngine):
    for relation in ("plants", "kpi_measurements", "alarms", "schema_migrations"):
        assert await relation_exists(migrated, relation), relation
    assert await is_hypertable(migrated, "kpi_measurements")


async def test_applying_the_real_migrations_twice_is_a_no_op(migrated: AsyncEngine):
    assert await apply_pending(migrated, MIGRATIONS_DIR, emit=lambda _: None) == 0


async def test_the_real_migrations_roll_back_and_forward_again(migrated: AsyncEngine):
    # An incident needs a way back that has actually been executed, not a
    # documented intention.
    await downgrade_to(migrated, MIGRATIONS_DIR, "base", emit=lambda _: None)
    for relation in ("plants", "kpi_measurements", "alarms"):
        assert not await relation_exists(migrated, relation), relation
    async with migrated.connect() as conn:
        assert await conn.scalar(sa.text("SELECT count(*) FROM schema_migrations")) == 0

    await apply_pending(migrated, MIGRATIONS_DIR, emit=lambda _: None)
    assert await is_hypertable(migrated, "kpi_measurements")


async def test_rollback_keeps_the_timescaledb_extension(migrated: AsyncEngine):
    # Other databases in the cluster may depend on it, and re-creating it is
    # cheap while losing it is not.
    await downgrade_to(migrated, MIGRATIONS_DIR, "base", emit=lambda _: None)
    async with migrated.connect() as conn:
        assert await conn.scalar(
            sa.text("SELECT count(*) FROM pg_extension WHERE extname = 'timescaledb'")
        )


# --------------------------------------------------------------------- #
# ADR-006 / D2 — what TimescaleDB actually permits                      #
# --------------------------------------------------------------------- #


async def test_a_hypertable_may_reference_a_regular_table(migrated: AsyncEngine):
    """The direction PR-1 already depends on: kpi_measurements -> plants.

    PR-2A1's provenance columns need this to keep working, so it is pinned
    rather than assumed.
    """
    async with migrated.connect() as conn:
        constraint = await conn.scalar(
            sa.text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'kpi_measurements'::regclass AND contype = 'f'"
            )
        )
    assert constraint is not None


async def test_a_hypertable_cannot_be_keyed_without_its_partitioning_column(
    migrated: AsyncEngine,
):
    """The constraint that decides D2, established empirically rather than cited.

    A foreign key needs a UNIQUE constraint on the referenced columns. A
    TimescaleDB hypertable refuses any unique index that omits its partitioning
    column, so a surrogate ``id`` alone can never be unique on a hypertable —
    and therefore nothing can reference such a table by ``id``.

    Consequence for PR-2A1: if raw payloads are stored in a hypertable (so that
    retention is a chunk drop rather than a mass DELETE), then
    ``kpi_measurements.raw_payload_id`` and the quarantine tables must hold
    SOFT references — a plain BIGINT with no FK — and provenance for a purged
    payload must be reported as such rather than treated as corruption.
    """
    async with migrated.begin() as conn:
        await conn.execute(
            sa.text(
                "CREATE TABLE probe_raw ("
                "  id BIGINT GENERATED ALWAYS AS IDENTITY,"
                "  received_at TIMESTAMPTZ NOT NULL,"
                "  PRIMARY KEY (id, received_at))"
            )
        )
        await conn.execute(sa.text("SELECT create_hypertable('probe_raw', 'received_at')"))

    with pytest.raises(DBAPIError) as excinfo:
        async with migrated.begin() as conn:
            await conn.execute(sa.text("CREATE UNIQUE INDEX ON probe_raw (id)"))
    # The message names the partitioning column; asserting on it keeps this
    # test honest if a future TimescaleDB release changes the rule.
    assert "received_at" in str(excinfo.value)


async def test_a_regular_table_cannot_foreign_key_into_a_hypertable(migrated: AsyncEngine):
    """The only remaining way to reference a hypertable: its full composite key.

    Referencing ``id`` alone fails on PLAIN PostgreSQL too — "no unique
    constraint matching given keys" — so that form measures nothing about
    TimescaleDB. The composite primary key `(id, received_at)` does exist and
    is unique, so this is the case that actually decides whether a HARD
    reference into raw storage is possible at all.

    If this ever starts passing, PR-2A1 gains an option it does not have today:
    a composite foreign key, at the cost of carrying ``received_at`` in every
    referencing row. Until then, soft references stand.
    """
    async with migrated.begin() as conn:
        await conn.execute(
            sa.text(
                "CREATE TABLE probe_raw2 ("
                "  id BIGINT NOT NULL,"
                "  received_at TIMESTAMPTZ NOT NULL,"
                "  PRIMARY KEY (id, received_at))"
            )
        )
        await conn.execute(sa.text("SELECT create_hypertable('probe_raw2', 'received_at')"))

    with pytest.raises(DBAPIError) as excinfo:
        async with migrated.begin() as conn:
            await conn.execute(
                sa.text(
                    "CREATE TABLE probe_child ("
                    "  raw_id BIGINT NOT NULL,"
                    "  raw_received_at TIMESTAMPTZ NOT NULL,"
                    "  FOREIGN KEY (raw_id, raw_received_at)"
                    "    REFERENCES probe_raw2 (id, received_at))"
                )
            )
    # Not the generic "no unique constraint" error — that would mean the key was
    # never found, and this key exists. The refusal has to be about hypertables.
    assert "no unique constraint" not in str(excinfo.value).lower()


async def test_referencing_a_hypertable_by_id_alone_is_not_a_timescale_specific_result(
    migrated: AsyncEngine,
):
    """Guards the finding above against being restated in a form that proves nothing.

    Plain PostgreSQL rejects a foreign key to a non-unique column with exactly
    the same error, so a test written that way would pass for a reason that has
    nothing to do with TimescaleDB. Recording that here keeps the distinction
    from being lost the next time these probes are edited.
    """
    async with migrated.begin() as conn:
        await conn.execute(
            sa.text(
                "CREATE TABLE probe_plain ("
                "  id BIGINT NOT NULL,"
                "  received_at TIMESTAMPTZ NOT NULL,"
                "  PRIMARY KEY (id, received_at))"
            )
        )  # deliberately NOT a hypertable

    with pytest.raises(DBAPIError) as excinfo:
        async with migrated.begin() as conn:
            await conn.execute(
                sa.text("CREATE TABLE probe_plain_child (raw_id BIGINT REFERENCES probe_plain(id))")
            )
    assert "no unique constraint" in str(excinfo.value).lower()


# --------------------------------------------------------------------- #
# PostgresRepository against the real schema (first coverage it has had) #
# --------------------------------------------------------------------- #


def plant(code: str, **overrides) -> PlantInfo:
    values = {
        "vendor": "fusionsolar",
        "vendor_plant_id": code,
        "name": f"Plant {code}",
        "capacity_kwp": 1000.0,
        "address": "synthetic address",
    }
    values.update(overrides)
    return PlantInfo(**values)


def reading(code: str, ts: datetime, **overrides) -> PlantKpiReading:
    values = {
        "vendor": "fusionsolar",
        "vendor_plant_id": code,
        "ts": ts,
        "daily_energy_kwh": 100.0,
        "status": PlantStatus.HEALTHY,
    }
    values.update(overrides)
    return PlantKpiReading(**values)


@pytest.fixture
async def repo(migrated: AsyncEngine) -> PostgresRepository:
    return PostgresRepository(migrated)


async def test_plants_are_inserted_and_read_back(repo: PostgresRepository):
    stored = await repo.upsert_plants([plant("SITE-1"), plant("SITE-2")])
    assert {p.vendor_plant_id for p in stored} == {"SITE-1", "SITE-2"}
    assert [p.vendor_plant_id for p in await repo.list_plants()] == ["SITE-1", "SITE-2"]


async def test_upserting_the_same_plant_updates_it_without_duplicating(
    repo: PostgresRepository,
):
    (first,) = await repo.upsert_plants([plant("SITE-1")])
    (again,) = await repo.upsert_plants([plant("SITE-1", name="Renamed")])
    assert again.id == first.id
    assert again.name == "Renamed"
    assert len(await repo.list_plants()) == 1


async def test_an_unreported_capacity_never_erases_the_stored_one(repo: PostgresRepository):
    # PR-1's rule, now proven against real SQL rather than only the in-memory
    # backend: None means "not reported", not "the plant lost its capacity".
    await repo.upsert_plants([plant("SITE-1", capacity_kwp=1234.0)])
    (updated,) = await repo.upsert_plants([plant("SITE-1", capacity_kwp=None)])
    assert updated.capacity_kwp == 1234.0


async def test_kpi_writes_are_idempotent_on_plant_and_timestamp(repo: PostgresRepository):
    # The property PR-2B's replay will depend on: re-writing the same reading
    # must converge, not accumulate.
    await repo.upsert_plants([plant("SITE-1")])
    assert await repo.record_kpis([reading("SITE-1", T0)]) == 1
    assert await repo.record_kpis([reading("SITE-1", T0, daily_energy_kwh=999.0)]) == 0

    latest = await repo.latest_kpi((await repo.list_plants())[0].id)
    assert latest is not None and latest.daily_energy_kwh == 100.0  # the first write stands


async def test_readings_for_unknown_plants_are_skipped(repo: PostgresRepository):
    await repo.upsert_plants([plant("SITE-1")])
    written = await repo.record_kpis([reading("SITE-1", T0), reading("SITE-UNKNOWN", T0)])
    assert written == 1


async def test_history_is_bounded_and_ordered(repo: PostgresRepository):
    await repo.upsert_plants([plant("SITE-1")])
    await repo.record_kpis([reading("SITE-1", T0 + timedelta(minutes=i)) for i in range(5)])
    plant_id = (await repo.list_plants())[0].id

    points = await repo.kpi_history(plant_id, T0 + timedelta(minutes=1), T0 + timedelta(minutes=4))
    assert [p.ts for p in points] == [T0 + timedelta(minutes=i) for i in (1, 2, 3)]

    assert len(await repo.kpi_history(plant_id, T0, T0 + timedelta(hours=1), limit=2)) == 2
