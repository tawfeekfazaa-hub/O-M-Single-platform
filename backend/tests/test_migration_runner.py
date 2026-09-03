"""Migration runner behaviour, against a live PostgreSQL.

These use SYNTHETIC migration files in a temp directory rather than the real
ones: the runner's contract — ordering, checksums, locking, rollback — is
independent of what any particular migration does, and fixtures we control let
each rule be exercised in isolation. The real migrations are applied end to end
in test_db_schema.py.

No TimescaleDB feature is used here, so this module runs against plain
PostgreSQL as well.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.db.migrations import (
    MigrationChecksumError,
    MigrationError,
    MigrationLockError,
    apply_pending,
    checksum_of,
    discover,
    downgrade_to,
    status,
)

pytestmark = pytest.mark.dbtest


def write_pair(directory: Path, name: str, up: str, down: str | None) -> None:
    (directory / f"{name}.sql").write_text(up)
    if down is not None:
        (directory / f"{name}.down.sql").write_text(down)


@pytest.fixture
def migrations_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "migrations"
    directory.mkdir()
    write_pair(
        directory,
        "001_first",
        "CREATE TABLE widget (id INT PRIMARY KEY);",
        "DROP TABLE IF EXISTS widget;",
    )
    write_pair(
        directory,
        "002_second",
        "CREATE TABLE gadget (id INT PRIMARY KEY);",
        "DROP TABLE IF EXISTS gadget;",
    )
    return directory


async def table_exists(engine: AsyncEngine, name: str) -> bool:
    async with engine.connect() as conn:
        return bool(await conn.scalar(sa.text("SELECT to_regclass(:n) IS NOT NULL"), {"n": name}))


async def applied_names(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text("SELECT filename FROM schema_migrations ORDER BY filename")
        )
        return [r.filename for r in rows]


# --------------------------------------------------------------------- #
# applying                                                              #
# --------------------------------------------------------------------- #


async def test_pending_migrations_are_applied_in_filename_order(
    db_engine: AsyncEngine, migrations_dir: Path
):
    lines: list[str] = []
    assert await apply_pending(db_engine, migrations_dir, emit=lines.append) == 2

    assert lines == ["apply 001_first.sql", "apply 002_second.sql"]
    assert await table_exists(db_engine, "widget")
    assert await table_exists(db_engine, "gadget")
    assert await applied_names(db_engine) == ["001_first.sql", "002_second.sql"]


async def test_re_running_is_a_no_op(db_engine: AsyncEngine, migrations_dir: Path):
    # "Safe to re-run" is the property an operator relies on when a deploy is
    # retried; a second apply must not attempt the DDL again.
    await apply_pending(db_engine, migrations_dir)
    lines: list[str] = []
    assert await apply_pending(db_engine, migrations_dir, emit=lines.append) == 0
    assert lines == ["skip  001_first.sql", "skip  002_second.sql"]


async def test_a_new_migration_is_applied_without_touching_the_earlier_ones(
    db_engine: AsyncEngine, migrations_dir: Path
):
    await apply_pending(db_engine, migrations_dir)
    write_pair(
        migrations_dir,
        "003_third",
        "CREATE TABLE sprocket (id INT PRIMARY KEY);",
        "DROP TABLE IF EXISTS sprocket;",
    )
    assert await apply_pending(db_engine, migrations_dir) == 1
    assert await applied_names(db_engine) == [
        "001_first.sql",
        "002_second.sql",
        "003_third.sql",
    ]


# --------------------------------------------------------------------- #
# immutability of applied migrations                                    #
# --------------------------------------------------------------------- #


async def test_editing_an_applied_migration_refuses_the_whole_run(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # The point of the checksum: what production ran must stay knowable. An
    # edited file means the recorded history no longer describes the database.
    await apply_pending(db_engine, migrations_dir)
    (migrations_dir / "001_first.sql").write_text("CREATE TABLE widget (id INT, extra TEXT);")
    write_pair(
        migrations_dir, "003_third", "CREATE TABLE sprocket (id INT);", "DROP TABLE sprocket;"
    )

    with pytest.raises(MigrationChecksumError, match="001_first.sql"):
        await apply_pending(db_engine, migrations_dir)

    # ... and the pending migration was NOT applied: the run is refused whole.
    assert not await table_exists(db_engine, "sprocket")
    assert await applied_names(db_engine) == ["001_first.sql", "002_second.sql"]


async def test_deleting_an_applied_migration_refuses_the_whole_run(
    db_engine: AsyncEngine, migrations_dir: Path
):
    await apply_pending(db_engine, migrations_dir)
    (migrations_dir / "002_second.sql").unlink()
    with pytest.raises(MigrationChecksumError, match="002_second.sql"):
        await apply_pending(db_engine, migrations_dir)


async def test_whitespace_only_line_endings_do_not_trip_the_checksum(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # A Windows checkout can rewrite LF to CRLF without changing one SQL
    # statement; a byte-level hash would refuse every run after that.
    await apply_pending(db_engine, migrations_dir)
    path = migrations_dir / "001_first.sql"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert await apply_pending(db_engine, migrations_dir) == 0


async def test_a_row_from_the_pre_checksum_runner_is_adopted_not_rejected(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # PR-1's runner recorded (filename, applied_at) only. Upgrading must not
    # brick an existing deployment, but the adoption has to be announced —
    # the original content is unknowable, so this is a baseline, not proof.
    await apply_pending(db_engine, migrations_dir)
    async with db_engine.begin() as conn:
        await conn.execute(sa.text("UPDATE schema_migrations SET checksum = NULL"))

    lines: list[str] = []
    assert await apply_pending(db_engine, migrations_dir, emit=lines.append) == 0
    assert [line for line in lines if line.startswith("adopt")] == [
        "adopt 001_first.sql  (checksum recorded for a pre-checksum row)",
        "adopt 002_second.sql  (checksum recorded for a pre-checksum row)",
    ]

    async with db_engine.connect() as conn:
        stored = await conn.scalar(
            sa.text("SELECT checksum FROM schema_migrations WHERE filename = '001_first.sql'")
        )
    assert stored == checksum_of(migrations_dir / "001_first.sql")


# --------------------------------------------------------------------- #
# rollback                                                              #
# --------------------------------------------------------------------- #


async def test_down_to_unwinds_in_reverse_order(db_engine: AsyncEngine, migrations_dir: Path):
    await apply_pending(db_engine, migrations_dir)
    lines: list[str] = []
    assert await downgrade_to(db_engine, migrations_dir, "001_first.sql", emit=lines.append) == 1

    assert lines == ["down  002_second.sql"]
    assert await table_exists(db_engine, "widget")  # the target stays applied
    assert not await table_exists(db_engine, "gadget")
    assert await applied_names(db_engine) == ["001_first.sql"]


async def test_down_to_base_unwinds_everything(db_engine: AsyncEngine, migrations_dir: Path):
    await apply_pending(db_engine, migrations_dir)
    assert await downgrade_to(db_engine, migrations_dir, "base") == 2
    assert not await table_exists(db_engine, "widget")
    assert not await table_exists(db_engine, "gadget")
    assert await applied_names(db_engine) == []


async def test_forward_after_a_full_rollback_works(db_engine: AsyncEngine, migrations_dir: Path):
    # The round trip is the property that makes a rollback usable in an
    # incident: down, fix, forward again.
    await apply_pending(db_engine, migrations_dir)
    await downgrade_to(db_engine, migrations_dir, "base")
    assert await apply_pending(db_engine, migrations_dir) == 2
    assert await table_exists(db_engine, "widget")
    assert await table_exists(db_engine, "gadget")


async def test_a_missing_down_file_unwinds_nothing_at_all(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # Checked up front, for the WHOLE set: discovering a missing rollback
    # half-way would leave the schema in a state no file describes.
    write_pair(migrations_dir, "003_third", "CREATE TABLE sprocket (id INT);", None)
    await apply_pending(db_engine, migrations_dir)

    with pytest.raises(MigrationError, match="003_third.sql"):
        await downgrade_to(db_engine, migrations_dir, "base")

    assert await table_exists(db_engine, "widget")  # nothing was unwound
    assert await applied_names(db_engine) == [
        "001_first.sql",
        "002_second.sql",
        "003_third.sql",
    ]


async def test_down_to_an_unknown_target_is_refused(db_engine: AsyncEngine, migrations_dir: Path):
    await apply_pending(db_engine, migrations_dir)
    with pytest.raises(MigrationError, match="unknown --down-to target"):
        await downgrade_to(db_engine, migrations_dir, "099_typo.sql")


# --------------------------------------------------------------------- #
# concurrency and discovery                                             #
# --------------------------------------------------------------------- #


async def test_two_concurrent_runs_cannot_interleave(db_url: URL, migrations_dir: Path):
    # Two deploy jobs racing is the realistic case. The loser must fail loudly
    # rather than run the same DDL a second time.
    slow = create_async_engine(db_url)
    blocker = create_async_engine(db_url)
    try:
        held = asyncio.Event()
        release = asyncio.Event()

        async def hold_the_lock() -> None:
            async with blocker.connect() as conn:
                from app.db.migrations import ADVISORY_LOCK_KEY

                await conn.execute(sa.text("SELECT pg_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
                await conn.commit()
                held.set()
                await release.wait()

        task = asyncio.create_task(hold_the_lock())
        await held.wait()
        try:
            with pytest.raises(MigrationLockError):
                await apply_pending(slow, migrations_dir)
        finally:
            release.set()
            await task
    finally:
        await slow.dispose()
        await blocker.dispose()


async def test_the_lock_is_released_for_the_next_run(db_engine: AsyncEngine, migrations_dir: Path):
    await apply_pending(db_engine, migrations_dir)
    assert await apply_pending(db_engine, migrations_dir) == 0  # would raise if still held


def test_an_unorderable_filename_is_refused(tmp_path: Path):
    # Ordering is the entire contract of a numbered runner; a file that cannot
    # be ordered must not be guessed at.
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "add_thing.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="NNN_name.sql"):
        discover(directory)


def test_down_files_are_not_mistaken_for_migrations(migrations_dir: Path):
    assert [m.filename for m in discover(migrations_dir)] == [
        "001_first.sql",
        "002_second.sql",
    ]


async def test_status_reports_applied_and_pending(db_engine: AsyncEngine, migrations_dir: Path):
    await apply_pending(db_engine, migrations_dir)
    await downgrade_to(db_engine, migrations_dir, "001_first.sql")
    # Added while pending, and deliberately without a rollback file.
    write_pair(migrations_dir, "003_third", "CREATE TABLE sprocket (id INT);", None)

    states = await status(db_engine, migrations_dir)
    assert [(s.filename, s.applied, s.has_down) for s in states] == [
        ("001_first.sql", True, True),
        ("002_second.sql", False, True),
        ("003_third.sql", False, False),
    ]
