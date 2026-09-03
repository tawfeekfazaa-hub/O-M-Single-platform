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

from app.db import migrations as migrations_module
from app.db.migrations import (
    Migration,
    MigrationChecksumError,
    MigrationError,
    MigrationLockError,
    MigrationOrderError,
    apply_pending,
    discover,
    downgrade_to,
    read_migration,
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


async def test_a_row_from_the_pre_checksum_runner_is_refused_by_default(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # Recording the current file's hash would declare the database verified
    # against SQL that may never have run there — exactly the drift the
    # checksum exists to reveal. Adoption is the operator's call, not ours.
    await apply_pending(db_engine, migrations_dir)
    async with db_engine.begin() as conn:
        await conn.execute(sa.text("UPDATE schema_migrations SET checksum = NULL"))

    with pytest.raises(MigrationChecksumError, match="recorded no checksum"):
        await apply_pending(db_engine, migrations_dir)
    with pytest.raises(MigrationChecksumError, match="recorded no checksum"):
        await downgrade_to(db_engine, migrations_dir, "base")


async def test_legacy_rows_are_adopted_only_when_the_operator_asks(
    db_engine: AsyncEngine, migrations_dir: Path
):
    await apply_pending(db_engine, migrations_dir)
    async with db_engine.begin() as conn:
        await conn.execute(sa.text("UPDATE schema_migrations SET checksum = NULL"))

    lines: list[str] = []
    assert await apply_pending(db_engine, migrations_dir, emit=lines.append, adopt_legacy=True) == 0
    # The message says what the recorded baseline is worth: nothing was verified.
    assert [line for line in lines if line.startswith("adopt")] == [
        "adopt 001_first.sql  (unverified baseline recorded on operator request)",
        "adopt 002_second.sql  (unverified baseline recorded on operator request)",
    ]
    assert await apply_pending(db_engine, migrations_dir) == 0  # verifiable from now on


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


async def test_the_lock_is_released_to_other_processes_not_just_the_same_pool(
    db_engine: AsyncEngine, db_url: URL, migrations_dir: Path
):
    # A session-level lock left on a pooled connection keeps working for the
    # SAME engine — advisory locks are reentrant — while blocking everyone else.
    # Only a separate engine proves it was actually released.
    await apply_pending(db_engine, migrations_dir)

    other = create_async_engine(db_url)
    try:
        async with other.connect() as conn:
            acquired = await conn.scalar(
                sa.text("SELECT pg_try_advisory_lock(:k)"),
                {"k": migrations_module.ADVISORY_LOCK_KEY},
            )
        assert acquired is True
    finally:
        await other.dispose()


async def test_the_lock_is_released_even_when_a_migration_fails(
    db_engine: AsyncEngine, db_url: URL, migrations_dir: Path
):
    write_pair(migrations_dir, "003_broken", "THIS IS NOT SQL;", "SELECT 1;")
    with pytest.raises(MigrationError):
        await apply_pending(db_engine, migrations_dir)

    other = create_async_engine(db_url)
    try:
        async with other.connect() as conn:
            acquired = await conn.scalar(
                sa.text("SELECT pg_try_advisory_lock(:k)"),
                {"k": migrations_module.ADVISORY_LOCK_KEY},
            )
        assert acquired is True
    finally:
        await other.dispose()


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


# --------------------------------------------------------------------- #
# integrity of what actually runs                                       #
# --------------------------------------------------------------------- #


async def test_an_edited_rollback_file_is_refused(db_engine: AsyncEngine, migrations_dir: Path):
    # The dangerous case: a rollback is destructive, and the forward checksum
    # says nothing about it. A tampered down file was observed dropping a table
    # belonging to a DIFFERENT migration while every preflight check passed.
    await apply_pending(db_engine, migrations_dir)
    (migrations_dir / "002_second.down.sql").write_text(
        "DROP TABLE IF EXISTS gadget; DROP TABLE IF EXISTS widget;"
    )

    with pytest.raises(MigrationChecksumError, match="002_second.sql"):
        await downgrade_to(db_engine, migrations_dir, "001_first.sql")

    # Nothing ran: 001's table is untouched and 002 is still applied.
    assert await table_exists(db_engine, "widget")
    assert await table_exists(db_engine, "gadget")
    assert await applied_names(db_engine) == ["001_first.sql", "002_second.sql"]


async def test_a_rollback_file_added_after_the_fact_is_flagged_not_silently_trusted(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # Nothing was recorded to compare against, so it runs — but the operator is
    # told the difference between "verified" and "unverifiable".
    write_pair(migrations_dir, "003_third", "CREATE TABLE sprocket (id INT);", None)
    await apply_pending(db_engine, migrations_dir)
    (migrations_dir / "003_third.down.sql").write_text("DROP TABLE IF EXISTS sprocket;")

    lines: list[str] = []
    assert await downgrade_to(db_engine, migrations_dir, "002_second.sql", emit=lines.append) == 1
    assert "warn  003_third.sql  (rollback file was not recorded at apply time)" in lines
    assert not await table_exists(db_engine, "sprocket")


async def test_the_content_that_was_checksummed_is_the_content_that_runs(
    db_engine: AsyncEngine, migrations_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    # A deploy can replace a shared checkout mid-run. Re-reading the file at
    # execution time would record one checksum and execute different SQL, with
    # the divergence surfacing only on a future run — after the schema had
    # already drifted. Discovery reads once; that text is what executes.
    captured = read_migration(migrations_dir / "001_first.sql")
    planted = Migration(
        filename=captured.filename,
        path=captured.path,
        content="CREATE TABLE from_captured_content (id INT);",
        checksum=captured.checksum,
        down_content=captured.down_content,
        down_checksum=captured.down_checksum,
    )
    monkeypatch.setattr(migrations_module, "discover", lambda _directory: [planted])

    await apply_pending(db_engine, migrations_dir)

    assert await table_exists(db_engine, "from_captured_content")
    assert not await table_exists(db_engine, "widget")  # the on-disk file never ran


# --------------------------------------------------------------------- #
# history must stay a prefix                                            #
# --------------------------------------------------------------------- #


async def test_a_migration_numbered_behind_applied_ones_is_refused(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # 001 and 003 applied, then 002 appears. Applying it would put the real
    # execution order at 001, 003, 002 while the bookkeeping implies filename
    # order — and rollback unwinds in reverse filename order, so it would run
    # the down files in an order that never happened.
    held_up = (migrations_dir / "002_second.sql").read_text()
    held_down = (migrations_dir / "002_second.down.sql").read_text()
    (migrations_dir / "002_second.sql").unlink()
    (migrations_dir / "002_second.down.sql").unlink()
    write_pair(
        migrations_dir,
        "003_third",
        "CREATE TABLE sprocket (id INT);",
        "DROP TABLE IF EXISTS sprocket;",
    )
    await apply_pending(db_engine, migrations_dir)
    assert await applied_names(db_engine) == ["001_first.sql", "003_third.sql"]

    (migrations_dir / "002_second.sql").write_text(held_up)
    (migrations_dir / "002_second.down.sql").write_text(held_down)

    with pytest.raises(MigrationOrderError, match="002_second.sql"):
        await apply_pending(db_engine, migrations_dir)
    with pytest.raises(MigrationOrderError, match="002_second.sql"):
        await downgrade_to(db_engine, migrations_dir, "base")
    assert not await table_exists(db_engine, "gadget")


async def test_appending_a_migration_at_the_end_stays_a_prefix(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # The ordinary case must not be caught by the prefix rule.
    await apply_pending(db_engine, migrations_dir)
    write_pair(
        migrations_dir,
        "003_third",
        "CREATE TABLE sprocket (id INT);",
        "DROP TABLE IF EXISTS sprocket;",
    )
    assert await apply_pending(db_engine, migrations_dir) == 1


# --------------------------------------------------------------------- #
# status is a diagnostic, so it must show drift                         #
# --------------------------------------------------------------------- #


async def test_status_reports_an_edited_migration(db_engine: AsyncEngine, migrations_dir: Path):
    await apply_pending(db_engine, migrations_dir)
    (migrations_dir / "001_first.sql").write_text("CREATE TABLE widget (id INT, extra TEXT);")

    states = {s.filename: s for s in await status(db_engine, migrations_dir)}
    assert states["001_first.sql"].drift == "file edited after it was applied"
    assert states["002_second.sql"].drift is None


async def test_status_reports_an_edited_rollback_file(db_engine: AsyncEngine, migrations_dir: Path):
    await apply_pending(db_engine, migrations_dir)
    (migrations_dir / "002_second.down.sql").write_text("DROP TABLE IF EXISTS everything;")

    states = {s.filename: s for s in await status(db_engine, migrations_dir)}
    assert states["002_second.sql"].drift == "rollback file edited after it was applied"


async def test_status_still_reports_an_applied_migration_whose_file_is_gone(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # This one used to vanish from the report entirely — the drift an operator
    # is least likely to notice unaided.
    await apply_pending(db_engine, migrations_dir)
    (migrations_dir / "002_second.sql").unlink()

    states = {s.filename: s for s in await status(db_engine, migrations_dir)}
    assert states["002_second.sql"].applied is True
    assert states["002_second.sql"].drift == "applied but the file is missing"


async def test_status_reports_an_unverifiable_legacy_row(
    db_engine: AsyncEngine, migrations_dir: Path
):
    await apply_pending(db_engine, migrations_dir)
    async with db_engine.begin() as conn:
        await conn.execute(sa.text("UPDATE schema_migrations SET checksum = NULL"))

    states = {s.filename: s for s in await status(db_engine, migrations_dir)}
    assert states["001_first.sql"].drift == "applied without a checksum (unverifiable)"


async def test_a_failing_migration_stays_inside_the_error_taxonomy(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # Migration SQL runs on the raw driver connection, so SQLAlchemy's exception
    # translation does not apply: without this the CLI would print a driver
    # traceback instead of a refusal, and the message could carry SQL.
    write_pair(migrations_dir, "003_broken", "THIS IS NOT SQL;", "SELECT 1;")

    with pytest.raises(MigrationError) as excinfo:
        await apply_pending(db_engine, migrations_dir)

    message = str(excinfo.value)
    assert "003_broken.sql failed to execute" in message
    assert "THIS IS NOT SQL" not in message  # names and types only, never content

    # The two good migrations stand; the broken one recorded nothing.
    assert await applied_names(db_engine) == ["001_first.sql", "002_second.sql"]
