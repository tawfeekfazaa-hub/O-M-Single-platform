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

import argparse
import asyncio
import contextlib
import re
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

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

    with pytest.raises(MigrationChecksumError, match="edited after being applied: 001_first.sql"):
        await apply_pending(db_engine, migrations_dir)

    # ... and the pending migration was NOT applied: the run is refused whole.
    assert not await table_exists(db_engine, "sprocket")
    assert await applied_names(db_engine) == ["001_first.sql", "002_second.sql"]


async def test_deleting_an_applied_migration_refuses_the_whole_run(
    db_engine: AsyncEngine, migrations_dir: Path
):
    await apply_pending(db_engine, migrations_dir)
    (migrations_dir / "002_second.sql").unlink()
    with pytest.raises(MigrationChecksumError, match="no longer present: 002_second.sql"):
        await apply_pending(db_engine, migrations_dir)


async def test_a_line_ending_change_to_an_applied_migration_is_drift(
    db_engine: AsyncEngine, migrations_dir: Path
):
    """Replaces a test that asserted the opposite, and proved nothing either way.

    It rewrote LF to CRLF in a fixture that had no newline in it, so the
    mutation changed zero bytes and the run passed because nothing had happened
    — while its comment described the newline-normalized checksum, a policy
    withdrawn when it turned out to let two files that insert different data
    share a hash.

    Under the exact-byte policy a line-ending change to an applied migration is
    drift, and drift is refused. `.gitattributes` is what stops a checkout
    producing one.
    """
    multiline = "CREATE TABLE widget (id INT PRIMARY KEY);\nCREATE TABLE extra (id INT);"
    (migrations_dir / "001_first.sql").write_text(multiline)
    await apply_pending(db_engine, migrations_dir, emit=lambda _: None)

    path = migrations_dir / "001_first.sql"
    rewritten = path.read_bytes().replace(b"\n", b"\r\n")
    assert rewritten != path.read_bytes()  # the mutation must actually mutate
    path.write_bytes(rewritten)

    with pytest.raises(MigrationChecksumError, match="edited after being applied: 001_first.sql"):
        await apply_pending(db_engine, migrations_dir, emit=lambda _: None)


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

    with pytest.raises(MigrationError, match="have no .down.sql: 003_third.sql"):
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
    with pytest.raises(MigrationError, match="003_broken.sql failed to execute"):
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

    with pytest.raises(MigrationChecksumError, match="rollback files were edited.* 002_second.sql"):
        await downgrade_to(db_engine, migrations_dir, "001_first.sql")

    # Nothing ran: 001's table is untouched and 002 is still applied.
    assert await table_exists(db_engine, "widget")
    assert await table_exists(db_engine, "gadget")
    assert await applied_names(db_engine) == ["001_first.sql", "002_second.sql"]


async def test_a_rollback_file_added_after_the_fact_is_refused_until_adopted(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # Nothing was recorded to compare against, so it is not run on a warning:
    # the operator has to look at it and say so.
    write_pair(migrations_dir, "003_third", "CREATE TABLE sprocket (id INT);", None)
    await apply_pending(db_engine, migrations_dir)
    (migrations_dir / "003_third.down.sql").write_text("DROP TABLE IF EXISTS sprocket;")

    # Refused by default: a recorded NULL is evidence this file did not
    # accompany the applied migration, and it can contain anything.
    with pytest.raises(MigrationChecksumError, match="has\nsince appeared|since appeared"):
        await downgrade_to(db_engine, migrations_dir, "002_second.sql")
    assert await table_exists(db_engine, "sprocket")

    lines: list[str] = []
    assert (
        await downgrade_to(
            db_engine, migrations_dir, "002_second.sql", emit=lines.append, adopt_legacy=True
        )
        == 1
    )
    assert "adopt 003_third.sql  (unverified rollback executed on operator request)" in lines
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

    with pytest.raises(MigrationOrderError, match="not a prefix.*002_second.sql"):
        await apply_pending(db_engine, migrations_dir)
    with pytest.raises(MigrationOrderError, match="not a prefix.*002_second.sql"):
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

    # Matched WHOLE, not searched for a phrase that must be absent: a blacklist
    # only catches the leak you thought of, and this one is a security property.
    # The pattern admits a filename and a bare exception type and nothing else,
    # so any SQL, value, host or port reaching the message fails it.
    assert re.fullmatch(r"003_broken\.sql failed to execute: [A-Za-z_]\w*", str(excinfo.value)), (
        f"unexpected shape: {str(excinfo.value)!r}"
    )

    # The two good migrations stand; the broken one recorded nothing.
    assert await applied_names(db_engine) == ["001_first.sql", "002_second.sql"]


async def test_a_migration_and_its_bookkeeping_row_commit_together(
    db_engine: AsyncEngine, migrations_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    # SQLAlchemy's transaction is lazy: conn.begin() creates the transaction
    # object, but asyncpg opens the real one only when a statement goes through
    # the adapter. Reaching for the driver first ran the migration in its own
    # implicit transaction, so it committed even when the bookkeeping INSERT
    # that follows failed — leaving a schema advanced with no history, which the
    # next run would then try to apply a second time.
    real_execute = migrations_module.AsyncConnection.execute
    calls = {"n": 0}

    async def fail_on_bookkeeping(self, statement, *args, **kwargs):
        if "INSERT INTO" in str(statement) and "schema_migrations" in str(statement):
            calls["n"] += 1
            raise RuntimeError("simulated bookkeeping failure")
        return await real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(migrations_module.AsyncConnection, "execute", fail_on_bookkeeping)

    with pytest.raises(RuntimeError, match="simulated bookkeeping failure"):
        await apply_pending(db_engine, migrations_dir)
    assert calls["n"] == 1

    monkeypatch.undo()
    # The DDL must have rolled back with it: no table, no history row.
    assert not await table_exists(db_engine, "widget")
    assert await applied_names(db_engine) == []


async def test_an_edited_rollback_file_blocks_applying_anything_new(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # Letting the schema advance while its recovery path is known to be corrupt
    # is exactly the moment a rollback is most likely to be needed.
    await apply_pending(db_engine, migrations_dir)
    (migrations_dir / "001_first.down.sql").write_text("DROP TABLE IF EXISTS widget CASCADE;")
    write_pair(
        migrations_dir,
        "003_third",
        "CREATE TABLE sprocket (id INT);",
        "DROP TABLE IF EXISTS sprocket;",
    )

    with pytest.raises(MigrationChecksumError, match="rollback files were edited.* 001_first.sql"):
        await apply_pending(db_engine, migrations_dir)
    assert not await table_exists(db_engine, "sprocket")


async def test_a_removed_rollback_file_is_treated_like_an_edited_one(
    db_engine: AsyncEngine, migrations_dir: Path
):
    await apply_pending(db_engine, migrations_dir)
    (migrations_dir / "002_second.down.sql").unlink()

    with pytest.raises(MigrationChecksumError, match="rollback files were edited.* 002_second.sql"):
        await apply_pending(db_engine, migrations_dir)


async def test_status_reports_a_history_that_is_not_a_prefix(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # apply and rollback both refuse this state, so a status that called it
    # clean and exited 0 would be worse than useless as a deployment gate.
    held_up = (migrations_dir / "002_second.sql").read_text()
    held_down = (migrations_dir / "002_second.down.sql").read_text()
    (migrations_dir / "002_second.sql").unlink()
    (migrations_dir / "002_second.down.sql").unlink()
    write_pair(
        migrations_dir, "003_third", "CREATE TABLE sprocket (id INT);", "DROP TABLE sprocket;"
    )
    await apply_pending(db_engine, migrations_dir)
    (migrations_dir / "002_second.sql").write_text(held_up)
    (migrations_dir / "002_second.down.sql").write_text(held_down)

    states = {s.filename: s for s in await status(db_engine, migrations_dir)}

    # Both halves of the disagreement are reported, and each is described as
    # what it actually is. The earlier version of this test asserted only that
    # every drift string contained "out of sequence", which let the pending
    # file be labelled "applied out of sequence" — contradicting the `applied`
    # flag on the same row.
    assert states["003_third.sql"].applied
    assert states["003_third.sql"].drift == "applied out of sequence (history is not a prefix)"

    assert not states["002_second.sql"].applied
    assert states["002_second.sql"].drift == (
        "pending behind an applied migration (history is not a prefix)"
    )

    assert states["001_first.sql"].drift is None  # the part of history that is fine


async def test_status_reports_a_rollback_file_that_appeared_or_vanished(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # Comparing only non-null checksums missed both directions: a recorded
    # rollback that is now gone, and one that appeared afterwards.
    write_pair(migrations_dir, "003_third", "CREATE TABLE sprocket (id INT);", None)
    await apply_pending(db_engine, migrations_dir)

    (migrations_dir / "002_second.down.sql").unlink()  # recorded, now gone
    (migrations_dir / "003_third.down.sql").write_text("DROP TABLE sprocket;")  # appeared

    states = {s.filename: s for s in await status(db_engine, migrations_dir)}
    assert states["002_second.sql"].drift == "rollback file removed after it was applied"
    assert states["003_third.sql"].drift == "rollback file added after it was applied (unverified)"


# --------------------------------------------------------------------- #
# a migration must not be able to commit itself                         #
# --------------------------------------------------------------------- #


async def test_a_migration_that_ends_the_transaction_past_the_guard_is_refused(
    db_engine: AsyncEngine, migrations_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    # The text guard in read_migration() refuses the ordinary case, but it reads
    # SQL without parsing it. This is the guarantee underneath: whatever the
    # file did, the transaction the bookkeeping row depends on must still be
    # open. Planting the migration bypasses the guard, so the check under test
    # is the only thing that can catch it.
    captured = read_migration(migrations_dir / "001_first.sql")
    planted = Migration(
        filename=captured.filename,
        path=captured.path,
        content="CREATE TABLE widget (id INT); COMMIT;",
        checksum=captured.checksum,
        down_content=captured.down_content,
        down_checksum=captured.down_checksum,
    )
    monkeypatch.setattr(migrations_module, "discover", lambda _directory: [planted])

    with pytest.raises(MigrationError, match="ended the runner's transaction"):
        await apply_pending(db_engine, migrations_dir)

    # The DDL is committed and unrecoverable — that is why the refusal tells the
    # operator to reconcile by hand. What must NOT happen is a history row
    # implying the pairing held.
    assert await applied_names(db_engine) == []


async def test_a_self_committing_migration_never_reaches_the_database(
    db_engine: AsyncEngine, migrations_dir: Path
):
    write_pair(
        migrations_dir,
        "003_third",
        "BEGIN;\nCREATE TABLE sprocket (id INT);\nCOMMIT;",
        "DROP TABLE IF EXISTS sprocket;",
    )
    with pytest.raises(MigrationError, match="manages its own transaction"):
        await apply_pending(db_engine, migrations_dir)

    # Discovery refuses the whole run, so the migrations ahead of it are not
    # applied either — the same all-or-nothing posture as a checksum mismatch.
    assert not await table_exists(db_engine, "sprocket")
    assert not await table_exists(db_engine, "widget")


# --------------------------------------------------------------------- #
# cleanup must not bury the failure it is cleaning up after             #
# --------------------------------------------------------------------- #


async def test_a_lost_connection_reports_the_migration_failure_not_the_cleanup_failure(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # A database that goes away mid-migration — a restart, an idle-timeout
    # proxy, an OOM-killed backend — leaves every cleanup path with a dead
    # connection. Rolling back the migration's transaction then raises on the
    # way out and REPLACES the MigrationError, and the CLI catches only
    # MigrationError: the operator gets a traceback about a closed connection
    # instead of the refusal, with the real reason buried.
    write_pair(
        migrations_dir,
        "003_third",
        "CREATE TABLE sprocket (id INT);\nSELECT pg_terminate_backend(pg_backend_pid());",
        "DROP TABLE IF EXISTS sprocket;",
    )
    with pytest.raises(MigrationError, match="003_third.sql failed to execute"):
        await apply_pending(db_engine, migrations_dir, emit=lambda _: None)
    # Note: this kills the WORK connection. Since the lock moved to a session of
    # its own, _unlock now takes its ordinary path here, so this no longer
    # exercises the discard fallback — that is
    # test_the_lock_is_freed_when_its_own_session_dies below.


async def test_a_failure_releasing_the_lock_neither_masks_nor_hides_itself(
    db_engine: AsyncEngine, migrations_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    # _unlock runs in a `finally`, so anything it raises replaces the failure
    # that brought us here. Suppressing that must not mean swallowing it
    # silently either: the operator gets the real refusal, plus a note that the
    # lock was dealt with by discarding the connection.
    write_pair(
        migrations_dir,
        "003_third",
        "CREATE TABLE sprocket (id INT NOT AN INT);",  # deliberate syntax error
        "DROP TABLE IF EXISTS sprocket;",
    )

    async def refuse_to_roll_back(self, *args, **kwargs):
        raise RuntimeError("simulated cleanup failure")

    monkeypatch.setattr(migrations_module.AsyncConnection, "rollback", refuse_to_roll_back)

    lines: list[str] = []
    with pytest.raises(MigrationError, match="003_third.sql failed to execute"):
        await apply_pending(db_engine, migrations_dir, emit=lines.append)
    assert any("advisory lock could not be released cleanly" in line for line in lines)


async def test_the_lock_is_free_after_the_connection_was_discarded(
    db_url: URL, migrations_dir: Path
):
    # The point of discarding rather than re-raising: the next run must not find
    # the lock still held by a session nobody is using.
    write_pair(
        migrations_dir,
        "003_third",
        "SELECT pg_terminate_backend(pg_backend_pid());",
        "SELECT 1;",
    )
    first = create_async_engine(db_url)
    try:
        with pytest.raises(MigrationError, match="003_third.sql failed to execute"):
            await apply_pending(first, migrations_dir, emit=lambda _: None)
    finally:
        await first.dispose()

    second = create_async_engine(db_url)
    try:
        async with second.connect() as conn:
            assert await conn.scalar(
                sa.text("SELECT pg_try_advisory_lock(:k)"),
                {"k": migrations_module.ADVISORY_LOCK_KEY},
            )
    finally:
        await second.dispose()


# --------------------------------------------------------------------- #
# a rollback file written later, without rolling the migration back     #
# --------------------------------------------------------------------- #


async def test_a_rollback_file_added_later_can_be_adopted_while_staying_applied(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # Without this the operator is stranded: --status reports drift and exits 2
    # forever, apply changes nothing, and the only way to clear it is to roll
    # the migration back — destroying data to close a bookkeeping gap.
    write_pair(migrations_dir, "003_third", "CREATE TABLE sprocket (id INT);", None)
    await apply_pending(db_engine, migrations_dir, emit=lambda _: None)
    (migrations_dir / "003_third.down.sql").write_text("DROP TABLE IF EXISTS sprocket;")

    assert [s.drift for s in await status(db_engine, migrations_dir) if s.drift] == [
        "rollback file added after it was applied (unverified)"
    ]

    lines: list[str] = []
    await apply_pending(db_engine, migrations_dir, emit=lines.append, adopt_legacy=True)
    assert "adopt 003_third.sql  (rollback file recorded unverified on operator request)" in lines

    assert await table_exists(db_engine, "sprocket")  # still applied
    assert [s.drift for s in await status(db_engine, migrations_dir) if s.drift] == []

    # And having been vouched for, it is now usable as a rollback without the
    # flag — which is exactly what adopting it authorised.
    assert await downgrade_to(db_engine, migrations_dir, "002_second.sql", emit=lambda _: None) == 1
    assert not await table_exists(db_engine, "sprocket")


async def test_adopting_a_late_rollback_file_takes_the_operator_saying_so(
    db_engine: AsyncEngine, migrations_dir: Path
):
    write_pair(migrations_dir, "003_third", "CREATE TABLE sprocket (id INT);", None)
    await apply_pending(db_engine, migrations_dir, emit=lambda _: None)
    (migrations_dir / "003_third.down.sql").write_text("DROP TABLE IF EXISTS sprocket;")

    lines: list[str] = []
    await apply_pending(db_engine, migrations_dir, emit=lines.append)
    assert not any("adopt" in line for line in lines)
    assert [s.drift for s in await status(db_engine, migrations_dir) if s.drift] == [
        "rollback file added after it was applied (unverified)"
    ]


async def test_adoption_does_not_touch_a_rollback_that_was_recorded_all_along(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # The flag is for unverifiable history. It must not become a way to quietly
    # re-baseline a rollback file that WAS recorded and has since been edited —
    # that is the tampering case, and it stays refused.
    await apply_pending(db_engine, migrations_dir, emit=lambda _: None)
    (migrations_dir / "001_first.down.sql").write_text("DROP TABLE IF EXISTS widget CASCADE;")

    with pytest.raises(MigrationChecksumError, match="rollback files were edited.* 001_first.sql"):
        await apply_pending(db_engine, migrations_dir, emit=lambda _: None, adopt_legacy=True)


async def test_a_sql_standard_function_body_applies_end_to_end(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # The guard stands END down for a file containing BEGIN ATOMIC, which is
    # only defensible if such a migration really does run — otherwise the
    # carve-out is protecting SQL nobody could use anyway.
    write_pair(
        migrations_dir,
        "003_third",
        "CREATE FUNCTION doubled(n INT) RETURNS INT LANGUAGE SQL\n"
        "BEGIN ATOMIC\n"
        "  SELECT CASE WHEN n > 0 THEN n * 2 ELSE 0 END;\n"
        "END;",
        "DROP FUNCTION IF EXISTS doubled;",
    )
    assert await apply_pending(db_engine, migrations_dir, emit=lambda _: None) == 3

    async with db_engine.connect() as conn:
        assert await conn.scalar(sa.text("SELECT doubled(21)")) == 42

    # And the transaction survived it, which is what the carve-out relies on:
    # the history row is there, so the pairing held.
    assert "003_third.sql" in await applied_names(db_engine)


async def test_a_connection_lost_before_a_migration_starts_stays_in_the_taxonomy(
    db_engine: AsyncEngine, migrations_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    # The disconnect window the taxonomy used to miss: after the lock is taken,
    # before the migration's own SQL runs. The statement that opens the
    # transaction is the first thing to touch a connection that may already be
    # dead, and it sat outside the block that translates driver errors, so it
    # reached the CLI as a traceback instead of the documented refusal.
    real_execute = migrations_module.AsyncConnection.execute

    async def die_on_the_primer(self, statement, *args, **kwargs):
        if "standard_conforming_strings" in str(statement):
            raise DBAPIError("SET LOCAL ...", None, Exception("connection is closed"))
        return await real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(migrations_module.AsyncConnection, "execute", die_on_the_primer)

    with pytest.raises(MigrationError, match="could not be started"):
        await apply_pending(db_engine, migrations_dir, emit=lambda _: None)

    monkeypatch.undo()
    assert not await table_exists(db_engine, "widget")
    assert await applied_names(db_engine) == []


async def test_cancelling_a_run_mid_cleanup_still_frees_the_lock(
    db_url: URL, migrations_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    # asyncio.CancelledError inherits from BaseException, so an `except
    # Exception` around the unlock skips it entirely and the session goes back
    # to the pool still holding the lock. The same pool's next call then
    # succeeds reentrantly while every other process blocks — the leak is
    # invisible from inside the process that caused it.
    engine = create_async_engine(db_url)
    real_rollback = migrations_module.AsyncConnection.rollback

    async def cancel_before_the_unlock(self, *args, **kwargs):
        # _unlock's first await. Cancel, then yield so the CancelledError lands
        # here — before pg_advisory_unlock is ever sent.
        asyncio.current_task().cancel()
        await asyncio.sleep(0)
        return await real_rollback(self, *args, **kwargs)

    monkeypatch.setattr(migrations_module.AsyncConnection, "rollback", cancel_before_the_unlock)

    task = asyncio.create_task(apply_pending(engine, migrations_dir, emit=lambda _: None))
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.undo()

    # The caller's engine is still alive, as a library caller's would be.
    other = create_async_engine(db_url)
    try:
        async with other.connect() as conn:
            assert await conn.scalar(
                sa.text("SELECT pg_try_advisory_lock(:k)"),
                {"k": migrations_module.ADVISORY_LOCK_KEY},
            )
    finally:
        await other.dispose()
        await engine.dispose()


async def test_migrations_are_lexed_as_standard_strings_whatever_the_database_default(
    db_url: URL, migrations_dir: Path
):
    # standard_conforming_strings can be turned off per database or per role.
    # With it off, PostgreSQL honours backslash escapes in ORDINARY literals, so
    # 'it\'s' is one string and the COMMIT after it is real SQL — while the
    # guard reads the same text as two strings with the COMMIT inside the
    # second, and waves it through. Measured before the fix: both tables were
    # created and committed, with no history row.
    admin = create_async_engine(db_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(
                sa.text(f'ALTER DATABASE "{db_url.database}" SET standard_conforming_strings = off')
            )
    finally:
        await admin.dispose()

    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            assert await conn.scalar(sa.text("SHOW standard_conforming_strings")) == "off"

        write_pair(
            migrations_dir,
            "003_third",
            "CREATE TABLE note (body TEXT DEFAULT 'it\\'s');\nCOMMIT;\n"
            "CREATE TABLE smuggled (id INT);",
            "DROP TABLE IF EXISTS note;",
        )
        # Named, because the two assertions below are absences: they pass just as
        # well if the run failed for some unrelated reason. And the NAME is the
        # discriminator, not just a label — with the fix the server reads 'it\'
        # and `s'...` as the guard does, so no COMMIT exists and the file dies of
        # a syntax error while being EXECUTED. Without it the server honours the
        # backslash, the COMMIT is real, the file executes clean, and the failure
        # becomes the transaction-ended refusal instead. So reverting the fix
        # fails this line before it reaches the absences below.
        with pytest.raises(MigrationError, match=r"003_third\.sql failed to execute"):
            await apply_pending(engine, migrations_dir, emit=lambda _: None)

        # Nothing was smuggled past the guard, because the server lexed the file
        # the same way the guard did.
        assert not await table_exists(engine, "smuggled")
        assert not await table_exists(engine, "note")
    finally:
        await engine.dispose()


async def test_the_cli_reports_an_unreachable_database_as_a_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    # The documented contract is exit 2 with a reason. A server that is simply
    # not running used to print a ConnectionRefusedError traceback instead —
    # and that message carries the host and port, which nothing else in this
    # module lets into the output.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "apply_migrations_cli",
        Path(__file__).resolve().parents[1] / "scripts" / "apply_migrations.py",
    )
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    closed_port = "postgresql+asyncpg://nobody@127.0.0.1:1/nothing"
    monkeypatch.setattr(
        cli,
        "_parse_args",
        lambda: argparse.Namespace(status=False, down_to=None, adopt_legacy_checksums=False),
    )
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(database_url=closed_port))

    assert await cli.main() == 2
    err = capsys.readouterr().err
    # `"1" not in err.split("(")[0]` used to stand for "the port did not leak".
    # It inspects the text BEFORE the parenthesis, which is the constant
    # "migration refused: the run failed " — so it could not fail, whatever the
    # message carried. Measured: with `(ConnectionRefusedError: 127.0.0.2:1)`
    # substituted in, the old assertion still passed.
    #
    # A whitelist over the whole line instead. The parenthesis may hold a bare
    # exception type and nothing else, so a host, a port, a DSN or a driver
    # message all fail it — including ones nobody listed.
    assert re.fullmatch(r"migration refused: the run failed \([A-Za-z_]\w*\)\n", err), (
        f"unexpected shape: {err!r}"
    )


async def test_cancelling_while_the_lock_is_granted_still_frees_it(
    db_url: URL, migrations_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    # A narrower window than the cleanup one: PostgreSQL can grant the lock and
    # the caller be cancelled before the runner has arranged to release it. The
    # acquisition therefore has to happen INSIDE the region that releases it,
    # not before entering it.
    engine = create_async_engine(db_url)
    real_commit = migrations_module.AsyncConnection.commit

    async def cancel_right_after_the_grant(self, *args, **kwargs):
        # _lock's commit, immediately after pg_try_advisory_lock returned true.
        monkeypatch.undo()
        asyncio.current_task().cancel()
        await asyncio.sleep(0)
        return await real_commit(self, *args, **kwargs)

    monkeypatch.setattr(migrations_module.AsyncConnection, "commit", cancel_right_after_the_grant)

    task = asyncio.create_task(apply_pending(engine, migrations_dir, emit=lambda _: None))
    with pytest.raises(asyncio.CancelledError):
        await task

    other = create_async_engine(db_url)
    try:
        async with other.connect() as conn:
            assert await conn.scalar(
                sa.text("SELECT pg_try_advisory_lock(:k)"),
                {"k": migrations_module.ADVISORY_LOCK_KEY},
            )
    finally:
        await other.dispose()
        await engine.dispose()


async def test_the_cli_reports_a_malformed_database_url_as_a_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    # create_async_engine() raises synchronously for a malformed URL or an
    # unavailable dialect, so building the engine has to happen inside the
    # handler — a configuration mistake is exactly the failure an operator
    # should see as a refusal rather than a traceback.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "apply_migrations_cli",
        Path(__file__).resolve().parents[1] / "scripts" / "apply_migrations.py",
    )
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    monkeypatch.setattr(
        cli,
        "_parse_args",
        lambda: argparse.Namespace(status=False, down_to=None, adopt_legacy_checksums=False),
    )
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(database_url="not-a-url"))

    assert await cli.main() == 2
    assert capsys.readouterr().err.startswith("migration refused: the run failed (")


async def test_the_maintenance_connection_uses_the_database_it_was_given(
    test_database_url: URL, monkeypatch: pytest.MonkeyPatch
):
    """Drives the fixture from a database that is deliberately not ``postgres``.

    Two earlier versions of this test could not fail. The first asserted
    ``_admin_url(url).database == "their_db"`` — a call site that substituted
    ``postgres`` without going through the helper would have kept it green. The
    second spied on the fixture's own call but compared against
    TEST_DATABASE_URL's database, which on the machine it was written on WAS
    ``postgres``, so the substitution was invisible there (CI names its database
    aq_om_test, so CI would have caught it — a test that only works on some
    machines is not a test). Hence the scratch database: whatever the URL says,
    the fixture is handed a database that is definitely not the cluster's.

    The requirement is that a role able to reach the database it was given, but
    not the cluster's ``postgres``, can run the live suite — it used to fail
    every live test before the first one ran.
    """
    from tests import conftest

    scratch = f"aq_admin_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(test_database_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(sa.text(f'CREATE DATABASE "{scratch}"'))

        seen: list[str | None] = []
        real_create = conftest.create_async_engine

        def spy(url, *args, **kwargs):
            seen.append(getattr(url, "database", None))
            return real_create(url, *args, **kwargs)

        monkeypatch.setattr(conftest, "create_async_engine", spy)
        generator = conftest.db_url.__wrapped__(test_database_url.set(database=scratch))
        created = await generator.__anext__()
        try:
            # The maintenance engine is the first one the fixture builds.
            assert seen[0] == scratch
            assert created.database != scratch  # and it really did make a new one
        finally:
            with contextlib.suppress(StopAsyncIteration):
                await generator.__anext__()
    finally:
        async with admin.connect() as conn:
            await conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": scratch},
            )
            await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        await admin.dispose()


async def test_the_cli_reports_an_invalid_unrelated_setting_as_a_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    # get_settings() validates the WHOLE application config, so a bad value in
    # a variable a migration run never reads — a stale rate-limit setting, a
    # mistyped FUSIONSOLAR_MODE — raised before the run began and reached the
    # operator as a Pydantic traceback.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "apply_migrations_cli",
        Path(__file__).resolve().parents[1] / "scripts" / "apply_migrations.py",
    )
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    monkeypatch.setattr(
        cli,
        "_parse_args",
        lambda: argparse.Namespace(status=False, down_to=None, adopt_legacy_checksums=False),
    )
    # The real thing rather than a stand-in: a genuinely invalid value, with the
    # settings cache cleared so it is re-read.
    monkeypatch.setenv("FUSIONSOLAR_MODE", "nonsense")
    cli.get_settings.cache_clear()
    try:
        assert await cli.main() == 2
        assert capsys.readouterr().err.startswith("migration refused: the run failed (")
    finally:
        cli.get_settings.cache_clear()


async def test_rolling_back_to_a_migration_that_was_never_applied_is_refused(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # Filtering a target the database never reached produces an empty set, and
    # "rolled back 0 migration(s)" with exit 0 tells automation it is now at a
    # revision it has never been at.
    write_pair(
        migrations_dir, "003_third", "CREATE TABLE sprocket (id INT);", "DROP TABLE sprocket;"
    )
    await apply_pending(db_engine, migrations_dir, emit=lambda _: None)
    await downgrade_to(db_engine, migrations_dir, "001_first.sql", emit=lambda _: None)

    with pytest.raises(MigrationError, match="003_third.sql: it is not applied"):
        await downgrade_to(db_engine, migrations_dir, "003_third.sql", emit=lambda _: None)

    # The refusal says where the database actually is, so the operator does not
    # have to go and look.
    with pytest.raises(MigrationError, match="database is at 001_first.sql"):
        await downgrade_to(db_engine, migrations_dir, "002_second.sql", emit=lambda _: None)

    assert await applied_names(db_engine) == ["001_first.sql"]  # nothing was unwound


async def test_rolling_back_to_base_from_an_empty_database_is_still_fine(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # `base` is not a migration, so the new check must not catch it — unwinding
    # nothing to reach base is a correct no-op, not a false revision claim.
    assert await downgrade_to(db_engine, migrations_dir, "base", emit=lambda _: None) == 0


async def test_a_crlf_migration_stores_what_it_says(db_engine: AsyncEngine, migrations_dir: Path):
    # End to end, because the split between "normalize for the checksum" and
    # "execute the file" only matters at the point the value lands in a column.
    (migrations_dir / "003_third.sql").write_bytes(
        b"CREATE TABLE note (body TEXT);\r\nINSERT INTO note VALUES ('first\r\nsecond');"
    )
    (migrations_dir / "003_third.down.sql").write_bytes(b"DROP TABLE IF EXISTS note;")
    await apply_pending(db_engine, migrations_dir, emit=lambda _: None)

    async with db_engine.connect() as conn:
        assert await conn.scalar(sa.text("SELECT body FROM note")) == "first\r\nsecond"


async def test_an_adoption_that_gets_rolled_back_is_never_announced(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # _verify writes the adopted checksums, then _require_prefix can still
    # refuse the run — and the refusal takes the write with it. Announcing
    # inside that transaction told the operator, and any log parser, that a
    # baseline was recorded when the row is still NULL.
    write_pair(
        migrations_dir, "003_third", "CREATE TABLE sprocket (id INT);", "DROP TABLE sprocket;"
    )
    (migrations_dir / "002_second.sql").rename(migrations_dir / "002_second.sql.hidden")
    (migrations_dir / "002_second.down.sql").rename(migrations_dir / "002_second.down.sql.hidden")
    await apply_pending(db_engine, migrations_dir, emit=lambda _: None)

    async with db_engine.connect() as conn:
        await conn.execute(
            sa.text(
                "UPDATE schema_migrations SET checksum = NULL, down_checksum = NULL "
                "WHERE filename = '001_first.sql'"
            )
        )
        await conn.commit()

    # 002 reappearing makes the history stop being a prefix, so the run is
    # refused after _verify has already written the adoption.
    (migrations_dir / "002_second.sql.hidden").rename(migrations_dir / "002_second.sql")
    (migrations_dir / "002_second.down.sql.hidden").rename(migrations_dir / "002_second.down.sql")

    lines: list[str] = []
    with pytest.raises(MigrationOrderError):
        await apply_pending(db_engine, migrations_dir, emit=lines.append, adopt_legacy=True)

    assert [line for line in lines if line.startswith("adopt")] == []
    async with db_engine.connect() as conn:
        stored = await conn.scalar(
            sa.text("SELECT checksum FROM schema_migrations WHERE filename = '001_first.sql'")
        )
    assert stored is None  # what was announced and what is recorded now agree


async def test_an_adoption_that_commits_is_announced(db_engine: AsyncEngine, migrations_dir: Path):
    # The other half: buffering must not swallow the announcement when the
    # adoption does land.
    await apply_pending(db_engine, migrations_dir, emit=lambda _: None)
    async with db_engine.connect() as conn:
        await conn.execute(
            sa.text("UPDATE schema_migrations SET checksum = NULL WHERE filename = '001_first.sql'")
        )
        await conn.commit()

    lines: list[str] = []
    await apply_pending(db_engine, migrations_dir, emit=lines.append, adopt_legacy=True)
    assert "adopt 001_first.sql  (unverified baseline recorded on operator request)" in lines

    async with db_engine.connect() as conn:
        assert await conn.scalar(
            sa.text("SELECT checksum FROM schema_migrations WHERE filename = '001_first.sql'")
        )


async def test_an_unverified_rollback_is_announced_only_once_it_has_run(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # The adopt lines used to be printed for the whole doomed set before any of
    # it ran, so a run that failed on the first rollback still claimed the rest
    # had been executed.
    write_pair(migrations_dir, "003_third", "CREATE TABLE sprocket (id INT);", None)
    write_pair(migrations_dir, "004_fourth", "CREATE TABLE cog (id INT);", None)
    await apply_pending(db_engine, migrations_dir, emit=lambda _: None)
    (migrations_dir / "003_third.down.sql").write_text("DROP TABLE IF EXISTS sprocket;")
    (migrations_dir / "004_fourth.down.sql").write_text("DROP TABLE nonexistent_on_purpose;")

    lines: list[str] = []
    with pytest.raises(MigrationError, match="004_fourth.down.sql failed to execute"):
        await downgrade_to(
            db_engine, migrations_dir, "002_second.sql", emit=lines.append, adopt_legacy=True
        )

    # 004 is unwound first and fails, so 003 never ran — and must not be
    # reported as executed.
    assert not any("003_third" in line and "adopt" in line for line in lines)
    assert await table_exists(db_engine, "sprocket")


async def test_a_reporter_that_fails_does_not_turn_a_success_into_a_refusal(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # Every announcement is made after the work it describes has committed, so
    # an exception from the caller's emit — a closed stdout, a logging handler
    # that raises — used to reach the CLI's handler and print "migration
    # refused" for a run that had already succeeded.
    def broken(_line: str) -> None:
        raise BrokenPipeError("stdout is closed")

    assert await apply_pending(db_engine, migrations_dir, emit=broken) == 2
    assert await table_exists(db_engine, "widget")
    assert await applied_names(db_engine) == ["001_first.sql", "002_second.sql"]


async def test_a_reporter_that_fails_is_not_allowed_to_hide_a_real_refusal(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # The other direction: swallowing the reporter's exception must not swallow
    # the runner's own.
    write_pair(
        migrations_dir, "003_third", "CREATE TABLE sprocket (id INT NOT AN INT);", "SELECT 1;"
    )

    def broken(_line: str) -> None:
        raise BrokenPipeError("stdout is closed")

    with pytest.raises(MigrationError, match="003_third.sql failed to execute"):
        await apply_pending(db_engine, migrations_dir, emit=broken)


async def test_only_one_body_is_opened_per_routine_definition(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # End to end, because the point is that PostgreSQL uses the second END to
    # commit: the guard must refuse the file before it can.
    write_pair(
        migrations_dir,
        "003_third",
        "CREATE TABLE begin (x int);\n"
        "CREATE FUNCTION f() RETURNS int LANGUAGE SQL BEGIN ATOMIC SELECT x FROM begin atomic; "
        "END;\nEND;",
        "DROP TABLE IF EXISTS begin;",
    )
    with pytest.raises(MigrationError, match="manages its own transaction"):
        await apply_pending(db_engine, migrations_dir, emit=lambda _: None)
    assert not await table_exists(db_engine, "widget")  # the whole run is refused


async def test_a_migration_cannot_redirect_the_ledger_with_search_path(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # Migration SQL runs on this connection and may legitimately SET
    # search_path. If the bookkeeping statement is unqualified it then resolves
    # somewhere else: measured, a migration that created app.schema_migrations
    # and set the path wrote its history row there, leaving the real ledger
    # empty — so the next run would apply the migration a second time.
    write_pair(
        migrations_dir,
        "003_third",
        "CREATE SCHEMA app;\n"
        "CREATE TABLE app.schema_migrations ("
        " filename TEXT PRIMARY KEY, checksum TEXT, down_checksum TEXT,"
        " applied_at TIMESTAMPTZ NOT NULL DEFAULT now());\n"
        "SET search_path = app;",
        "DROP SCHEMA IF EXISTS app CASCADE;",
    )
    await apply_pending(db_engine, migrations_dir, emit=lambda _: None)

    async with db_engine.connect() as conn:
        assert await conn.scalar(sa.text("SELECT count(*) FROM public.schema_migrations")) == 3
        assert await conn.scalar(sa.text("SELECT count(*) FROM app.schema_migrations")) == 0

    # The real proof: a fresh run sees all three as applied, not as pending.
    assert await apply_pending(db_engine, migrations_dir, emit=lambda _: None) == 0


async def test_two_migrations_may_not_share_a_sequence_number(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # Filename order hides this from the prefix rule: `002_beta` sorts after
    # `002_alpha`, so the applied set stays a prefix and a second 002 applies.
    # "We are at 002" would then name two different schemas.
    write_pair(
        migrations_dir, "002_beta", "CREATE TABLE sprocket (id INT);", "DROP TABLE sprocket;"
    )

    with pytest.raises(MigrationOrderError, match="two migrations are numbered 002"):
        await apply_pending(db_engine, migrations_dir, emit=lambda _: None)
    assert not await table_exists(db_engine, "widget")  # nothing ran

    # And it is refused on the diagnostic path too, not only on apply.
    with pytest.raises(MigrationOrderError, match="two migrations are numbered 002"):
        await status(db_engine, migrations_dir)


async def test_the_ledger_delete_cannot_be_subverted_by_a_custom_equality(
    db_engine: AsyncEngine, migrations_dir: Path
):
    # Qualifying the ledger RELATION does not pin the OPERATOR. A down migration
    # that puts its own schema ahead of pg_catalog and defines =(text, text)
    # returning false left the rolled-back migration still recorded as applied;
    # one returning true would have deleted the entire history.
    write_pair(
        migrations_dir,
        "003_third",
        "CREATE TABLE sprocket (id INT);",
        "DROP TABLE IF EXISTS sprocket;\n"
        "CREATE SCHEMA shadow;\n"
        "CREATE FUNCTION shadow.always_false(text, text) RETURNS boolean "
        "LANGUAGE sql IMMUTABLE AS 'SELECT false';\n"
        "CREATE OPERATOR shadow.= (LEFTARG=text, RIGHTARG=text, FUNCTION=shadow.always_false);\n"
        "SET search_path = shadow, pg_catalog;",
    )
    await apply_pending(db_engine, migrations_dir, emit=lambda _: None)
    await downgrade_to(db_engine, migrations_dir, "002_second.sql", emit=lambda _: None)

    assert await applied_names(db_engine) == ["001_first.sql", "002_second.sql"]


async def test_the_advisory_lock_functions_come_from_pg_catalog(db_url: URL):
    # Any route that leaves a hostile search_path on the connection _lock uses
    # would otherwise let a migration-defined pg_try_advisory_lock(bigint)
    # satisfy the runner without a real lock ever being taken. Tested as the
    # property rather than through an exploit, because the dedicated lock
    # connection and the session restore both also close the known route — and
    # defence that nothing tests looks exactly like defence that works.
    engine = create_async_engine(db_url)
    other = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(sa.text("CREATE SCHEMA app"))
            await conn.execute(
                sa.text(
                    "CREATE FUNCTION app.pg_try_advisory_lock(bigint) RETURNS boolean "
                    "LANGUAGE sql AS 'SELECT true'"
                )
            )
            await conn.execute(sa.text("SET search_path = app, pg_catalog"))
            await conn.commit()

            await migrations_module._lock(conn)
            try:
                async with other.connect() as watcher:
                    # If the shadow function had satisfied _lock, no real lock
                    # would be held and this would succeed.
                    assert not await watcher.scalar(
                        sa.text("SELECT pg_catalog.pg_try_advisory_lock(:k)"),
                        {"k": migrations_module.ADVISORY_LOCK_KEY},
                    )
            finally:
                await migrations_module._unlock(conn, emit=lambda _: None)
    finally:
        await other.dispose()
        await engine.dispose()


async def test_a_migration_cannot_release_the_runners_lock(db_url: URL):
    # The lock used to live on the same session the migrations run on, so a
    # migration containing pg_advisory_unlock_all() released it without ending
    # its transaction — and a second runner could enter mid-migration.
    engine = create_async_engine(db_url)
    other = create_async_engine(db_url)
    directory = Path(str(db_url.database))  # placeholder, replaced below
    try:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            write_pair(
                directory,
                "001_unlock",
                "CREATE TABLE widget (id INT);\nSELECT pg_advisory_unlock_all();",
                "DROP TABLE IF EXISTS widget;",
            )
            taken: dict[str, bool | None] = {}
            real_run = migrations_module._run_sql

            async def spy(conn, sql, *, filename):
                await real_run(conn, sql, filename=filename)
                async with other.connect() as watcher:
                    taken["free"] = bool(
                        await watcher.scalar(
                            sa.text("SELECT pg_catalog.pg_try_advisory_lock(:k)"),
                            {"k": migrations_module.ADVISORY_LOCK_KEY},
                        )
                    )

            migrations_module._run_sql = spy
            try:
                await apply_pending(engine, directory, emit=lambda _: None)
            finally:
                migrations_module._run_sql = real_run
            assert taken["free"] is False
    finally:
        await other.dispose()
        await engine.dispose()


async def test_the_connection_is_returned_without_the_migrations_search_path(
    db_url: URL, migrations_dir: Path
):
    # A migration's SET search_path is committed with it and rides back into the
    # pool. A library caller's next query never reaches _ensure_bookkeeping's
    # reset, so it would resolve unqualified names in the migration's schema.
    write_pair(
        migrations_dir,
        "003_third",
        "CREATE SCHEMA app;\nSET search_path = app;",
        "DROP SCHEMA IF EXISTS app CASCADE;",
    )
    engine = create_async_engine(db_url)
    try:
        await apply_pending(engine, migrations_dir, emit=lambda _: None)
        async with engine.connect() as conn:
            # The setup has to have happened for the absence below to mean
            # anything: a migration that never ran leaves a clean session too.
            assert await conn.scalar(sa.text("SELECT to_regnamespace('app') IS NOT NULL"))
            assert "app" not in str(await conn.scalar(sa.text("SHOW search_path")))
    finally:
        await engine.dispose()


async def test_the_cli_survives_a_closed_stdout_on_a_successful_run(db_url: URL, tmp_path: Path):
    """Drives main() rather than say(), which is the point.

    The previous version of this test called `say()` directly, so replacing any
    `say(...)` call site with `print(...)` would have restored the bug while the
    test stayed green — it proved the helper works, not that it is used at the
    boundary it protects.
    """
    import argparse
    import importlib.util
    from types import SimpleNamespace

    directory = tmp_path / "migrations"
    directory.mkdir()
    write_pair(directory, "001_first", "CREATE TABLE widget (id INT);", "DROP TABLE widget;")

    spec = importlib.util.spec_from_file_location(
        "apply_migrations_cli_e2e",
        Path(__file__).resolve().parents[1] / "scripts" / "apply_migrations.py",
    )
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    cli.MIGRATIONS_DIR = directory
    cli._parse_args = lambda: argparse.Namespace(
        status=False, down_to=None, adopt_legacy_checksums=False
    )
    cli.get_settings = lambda: SimpleNamespace(database_url=str(db_url))

    class Closed:
        def write(self, *_args: object) -> int:
            raise BrokenPipeError("stdout is closed")

        def flush(self) -> None:
            raise BrokenPipeError("stdout is closed")

    real_stdout, sys.stdout = sys.stdout, Closed()
    try:
        exit_code = await cli.main()
    finally:
        sys.stdout = real_stdout

    assert exit_code == 0  # the run succeeded and says so
    engine = create_async_engine(db_url)
    try:
        assert await table_exists(engine, "widget")  # and it really did the work
    finally:
        await engine.dispose()


async def _run_killing_the_idle_lock_session(engine: AsyncEngine, migrations_dir: Path, emit):
    """Apply migrations, terminating the lock session in the middle of one.

    The kill happens AFTER the migration's own SQL, at which point the work
    connection is `idle in transaction` and the lock connection is the only
    `idle` one in this (per-test) database — so the terminate hits the lock
    session and nothing else.
    """
    killer = create_async_engine(engine.url, isolation_level="AUTOCOMMIT")
    real_run = migrations_module._run_sql

    async def run_then_kill(conn, sql, *, filename):
        await real_run(conn, sql, filename=filename)
        async with killer.connect() as k:
            killed = await k.scalar(
                sa.text(
                    "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                    "WHERE datname = :n AND state = 'idle' AND pid <> pg_backend_pid()"
                ),
                {"n": engine.url.database},
            )
        # Without this the test could pass having killed nothing at all.
        assert killed == 1, f"expected to terminate the lock session only, killed {killed}"

    migrations_module._run_sql = run_then_kill
    try:
        with pytest.raises(MigrationError) as raised:
            await apply_pending(engine, migrations_dir, emit=emit)
    finally:
        migrations_module._run_sql = real_run
        await killer.dispose()
    return raised.value


async def test_a_lock_session_that_dies_mid_run_stops_the_run(db_url: URL, migrations_dir: Path):
    # The lock connection is idle for as long as the migration takes, so an
    # idle_session_timeout or a proxy can close it — and a session-scoped
    # advisory lock dies with its session. Measured before the check: a second
    # runner took the key while the first carried on and recorded its history.
    engine = create_async_engine(db_url)
    try:
        error = await _run_killing_the_idle_lock_session(engine, migrations_dir, lambda _: None)
    finally:
        await engine.dispose()
    assert "run lock" in str(error)

    verify = create_async_engine(db_url)
    try:
        # Nothing recorded: the work is abandoned rather than committed under a
        # lock somebody else may now hold.
        assert await applied_names(verify) == []
        assert not await table_exists(verify, "widget")
    finally:
        await verify.dispose()


async def test_a_dead_lock_session_is_reported_rather_than_masking_the_failure(
    db_url: URL, migrations_dir: Path
):
    # Regression cover that moved: the work-connection kill above used to be
    # what reached _unlock's discard path, and since the lock got a session of
    # its own it no longer does. Killing the LOCK session is now the case where
    # releasing the lock cannot succeed, and the cleanup must be reported
    # alongside the real failure rather than replacing it.
    lines: list[str] = []
    engine = create_async_engine(db_url)
    try:
        error = await _run_killing_the_idle_lock_session(engine, migrations_dir, lines.append)
    finally:
        await engine.dispose()

    assert "run lock" in str(error)  # the real reason survived the cleanup
    assert [line for line in lines if line.startswith("note: the advisory lock could not be")], (
        f"expected a note about the failed release, got {lines}"
    )


async def test_a_migration_leaves_no_session_state_behind_at_all(db_url: URL, migrations_dir: Path):
    # search_path was the case review found; statement_timeout, SET ROLE, LISTEN
    # and temporary objects ride back into the pool the same way.
    write_pair(
        migrations_dir,
        "003_third",
        "SET statement_timeout = '1234ms';\nCREATE TEMP TABLE leftover (id INT);"
        "\nCREATE TABLE sprocket (id INT);",
        "DROP TABLE IF EXISTS sprocket;",
    )
    engine = create_async_engine(db_url)
    try:
        await apply_pending(engine, migrations_dir, emit=lambda _: None)
        # BOTH connections, held at once: the run checks out two and the order
        # the pool hands them back is its own business, so asking for one could
        # ask the lock connection — which never ran a migration and would look
        # clean however dirty the other is. (With the fix in place the work
        # connection is gone rather than clean, so both of these are fresh
        # backends. Reverting the fix to `RESET search_path` puts the dirty one
        # back in the pool and this fails, which is what it is here to catch.)
        async with engine.connect() as first, engine.connect() as second:
            for conn in (first, second):
                assert await conn.scalar(sa.text("SHOW statement_timeout")) == "0"
                assert not await conn.scalar(sa.text("SELECT to_regclass('leftover') IS NOT NULL"))
    finally:
        await engine.dispose()


async def test_the_engine_is_still_usable_after_a_run(db_url: URL, migrations_dir: Path):
    # Scrubbing the session instead of ending it looks like the tidier answer
    # and quietly breaks the caller's engine: DISCARD ALL runs DEALLOCATE ALL,
    # and asyncpg's per-connection statement cache then names prepared
    # statements the server has forgotten. Measured: 17 tests in this module
    # failed with `prepared statement "__asyncpg_stmt_1d__" does not exist`.
    engine = create_async_engine(db_url)
    try:
        assert await apply_pending(engine, migrations_dir, emit=lambda _: None) == 2
        # Both pooled connections at once, replaying a statement the runner
        # itself issued on its work connection (_ensure_bookkeeping's), because
        # that is the only thing a poisoned cache breaks on: a statement the
        # server has forgotten but asyncpg still has a name for. A second
        # apply_pending() does NOT do it — the pool hands the connections back
        # in the other order, so the poisoned one becomes the next run's LOCK
        # connection and never re-runs anything it had prepared. That version of
        # this test passed against the broken code.
        async with engine.connect() as first, engine.connect() as second:
            for conn in (first, second):
                await conn.execute(sa.text("RESET search_path"))
                assert await conn.scalar(sa.text("SELECT 1")) == 1
    finally:
        await engine.dispose()


async def test_a_second_ledger_is_never_created_beside_an_existing_one(
    db_url: URL, migrations_dir: Path
):
    """A migration can move the search_path the NEXT run resets to.

    `RESET search_path` restores the configured default, and `ALTER DATABASE
    ... SET search_path` outlives the session, the pool and the process.
    Measured on a fresh engine afterwards: RESET yielded `evil`, `CREATE TABLE
    IF NOT EXISTS schema_migrations` made a second, empty ledger there, and the
    run silently re-applied an already-applied migration — a data migration's
    row went in twice. An operator retargeting `ALTER ROLE ... SET search_path`
    arrives at the same place by accident.
    """
    write_pair(
        migrations_dir,
        "003_third",
        "CREATE SCHEMA elsewhere;\n"
        f'ALTER DATABASE "{db_url.database}" SET search_path = elsewhere;\n'
        "CREATE TABLE IF NOT EXISTS reading (v INT);\nINSERT INTO public.reading VALUES (1);",
        "DROP TABLE IF EXISTS reading;",
    )
    engine = create_async_engine(db_url)
    try:
        assert await apply_pending(engine, migrations_dir, emit=lambda _: None) == 3
    finally:
        await engine.dispose()

    # A brand-new engine: nothing is left over on a pooled session, so the only
    # thing carrying the redirection is the database's own default.
    engine = create_async_engine(db_url)
    try:
        with pytest.raises(MigrationError, match="not on this connection's search_path"):
            await apply_pending(engine, migrations_dir, emit=lambda _: None)
        async with engine.connect() as conn:
            # Nothing applied twice, and no second ledger to make it look unapplied.
            assert await conn.scalar(sa.text("SELECT count(*) FROM public.reading")) == 1
            assert (
                await conn.scalar(
                    sa.text(
                        "SELECT count(*) FROM pg_catalog.pg_class c "
                        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE c.relname = 'schema_migrations' AND c.relkind = 'r'"
                    )
                )
                == 1
            )
    finally:
        await engine.dispose()


async def test_a_ledger_reachable_only_later_on_the_path_is_used_not_duplicated(
    db_url: URL, migrations_dir: Path
):
    # The same redirection, but with the ledger's schema still on the path
    # behind the new one. `CREATE TABLE IF NOT EXISTS` checks only the schema it
    # would create in — the FIRST on the path — not visibility, so it made a
    # duplicate in `elsewhere` while `public.schema_migrations` sat right there
    # and was perfectly visible. Nothing is refused here: the ledger resolves,
    # so the run simply carries on.
    write_pair(
        migrations_dir,
        "003_third",
        "CREATE SCHEMA elsewhere;\n"
        f'ALTER DATABASE "{db_url.database}" SET search_path = elsewhere, public;\n'
        "CREATE TABLE IF NOT EXISTS reading (v INT);\nINSERT INTO public.reading VALUES (1);",
        "DROP TABLE IF EXISTS reading;",
    )
    engine = create_async_engine(db_url)
    try:
        assert await apply_pending(engine, migrations_dir, emit=lambda _: None) == 3
    finally:
        await engine.dispose()

    engine = create_async_engine(db_url)
    try:
        assert await apply_pending(engine, migrations_dir, emit=lambda _: None) == 0
        async with engine.connect() as conn:
            assert await conn.scalar(sa.text("SELECT count(*) FROM public.reading")) == 1
            assert not await conn.scalar(
                sa.text("SELECT to_regclass('elsewhere.schema_migrations') IS NOT NULL")
            )
    finally:
        await engine.dispose()


async def test_an_engine_that_cannot_supply_two_connections_says_so(
    db_url: URL, migrations_dir: Path
):
    # The lock needs a session of its own, which is a pool requirement the
    # caller never agreed to. It used to surface as a pool timeout after
    # pool_timeout seconds, with no indication of why.
    engine = create_async_engine(db_url, pool_size=1, max_overflow=0, pool_timeout=2)
    try:
        with pytest.raises(MigrationError, match="fewer than two"):
            await apply_pending(engine, migrations_dir, emit=lambda _: None)
    finally:
        await engine.dispose()


async def test_a_temporary_table_cannot_shadow_the_ledger(db_url: URL, migrations_dir: Path):
    # PostgreSQL searches the session's implicit pg_temp schema before
    # search_path for RELATION names -- after RESET too -- and a temporary table
    # has relkind 'r' like any other. Measured before the fix, on an engine
    # whose pooled connections carried one: an empty history, both migrations
    # re-applied, the data migration's row inserted TWICE, exit 0.
    write_pair(migrations_dir, "003_third", "INSERT INTO widget (id) VALUES (1);", None)
    engine = create_async_engine(db_url, pool_size=2, max_overflow=0)
    try:
        assert await apply_pending(engine, migrations_dir, emit=lambda _: None) == 3

        # BOTH pooled connections, because the pool decides which becomes the
        # work connection and only that one matters. Planting it on one and
        # trusting FIFO is how the first version of this passed while proving
        # nothing -- the poisoned connection came back as the lock connection.
        async with engine.connect() as first, engine.connect() as second:
            for conn in (first, second):
                await conn.execute(
                    sa.text(
                        "CREATE TEMP TABLE schema_migrations ("
                        " filename TEXT PRIMARY KEY, checksum TEXT, down_checksum TEXT,"
                        " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                    )
                )
                await conn.commit()

        with pytest.raises(MigrationError, match="TEMPORARY table named schema_migrations"):
            await apply_pending(engine, migrations_dir, emit=lambda _: None)
    finally:
        await engine.dispose()

    verify = create_async_engine(db_url)
    try:
        # The real history is intact and the data migration ran exactly once.
        assert await applied_names(verify) == ["001_first.sql", "002_second.sql", "003_third.sql"]
        async with verify.connect() as conn:
            assert await conn.scalar(sa.text("SELECT count(*) FROM widget")) == 1
    finally:
        await verify.dispose()


async def test_a_migration_another_run_recorded_first_is_rolled_back(
    db_url: URL, migrations_dir: Path
):
    # _confirm_lock closes most of the window, not all of it: the lock session
    # can be closed between that check and this transaction's COMMIT, letting a
    # second runner take the key while this work is still uncommitted. `filename`
    # is the ledger's primary key, so the loser's INSERT conflicts -- and raising
    # discards its copy of the migration SQL along with it.
    write_pair(migrations_dir, "003_third", "CREATE TABLE sprocket (id INT);", None)
    engine = create_async_engine(db_url)
    interloper = create_async_engine(db_url, isolation_level="AUTOCOMMIT")
    real_run = migrations_module._run_sql

    async def record_it_from_elsewhere(conn, sql, *, filename):
        await real_run(conn, sql, filename=filename)
        if filename != "003_third.sql":
            return
        async with interloper.connect() as other:
            await other.execute(
                sa.text(
                    "INSERT INTO schema_migrations (filename, checksum, down_checksum) "
                    "VALUES (:f, 'other-run', 'other-run')"
                ),
                {"f": filename},
            )

    migrations_module._run_sql = record_it_from_elsewhere
    try:
        with pytest.raises(MigrationError, match="recorded by another run"):
            await apply_pending(engine, migrations_dir, emit=lambda _: None)
    finally:
        migrations_module._run_sql = real_run
        await interloper.dispose()
        await engine.dispose()

    verify = create_async_engine(db_url)
    try:
        # This run's copy of the work is gone; the other run's row stands.
        assert not await table_exists(verify, "sprocket")
        async with verify.connect() as conn:
            stored = await conn.scalar(
                sa.text("SELECT checksum FROM schema_migrations WHERE filename = :f"),
                {"f": "003_third.sql"},
            )
            assert stored == "other-run"
    finally:
        await verify.dispose()


async def test_a_rollback_another_run_already_recorded_is_undone(db_url: URL, migrations_dir: Path):
    # The mirror of the INSERT conflict, and the dangerous half: a DELETE that
    # matches nothing raises nothing. Measured before the fix -- with the lock
    # session closed after _confirm_lock passed, a second runner read the row as
    # still applied, ran the SAME down file again, deleted nothing and committed:
    # `times the rollback SQL executed: 2`, both runs reporting success.
    write_pair(
        migrations_dir,
        "003_third",
        "CREATE TABLE rollback_log (n INT);\nINSERT INTO rollback_log (n) VALUES (0);",
        "DROP TABLE IF EXISTS rollback_log;",
    )
    # Deliberately NOT idempotent, so a second execution is visible.
    write_pair(
        migrations_dir,
        "004_fourth",
        "CREATE TABLE cog (id INT);",
        "UPDATE rollback_log SET n = n + 1;\nDROP TABLE IF EXISTS cog;",
    )
    engine = create_async_engine(db_url)
    interloper = create_async_engine(db_url, isolation_level="AUTOCOMMIT")
    await apply_pending(engine, migrations_dir, emit=lambda _: None)
    real_run = migrations_module._run_sql

    async def unrecord_it_from_elsewhere(conn, sql, *, filename):
        await real_run(conn, sql, filename=filename)
        if filename != "004_fourth.down.sql":
            return
        async with interloper.connect() as other:
            await other.execute(
                sa.text("DELETE FROM schema_migrations WHERE filename = '004_fourth.sql'")
            )

    migrations_module._run_sql = unrecord_it_from_elsewhere
    try:
        with pytest.raises(MigrationError, match="already removed from the history"):
            await downgrade_to(engine, migrations_dir, "003_third.sql", emit=lambda _: None)
    finally:
        migrations_module._run_sql = real_run
        await interloper.dispose()
        await engine.dispose()

    verify = create_async_engine(db_url)
    try:
        async with verify.connect() as conn:
            # The duplicate rollback shared the transaction that was refused, so
            # it went with it: the counter never moved and the table it would
            # have dropped is still here.
            assert await conn.scalar(sa.text("SELECT n FROM rollback_log")) == 0
        assert await table_exists(verify, "cog")
    finally:
        await verify.dispose()


async def test_the_lock_session_is_not_parked_in_a_transaction(db_url: URL, migrations_dir: Path):
    # _confirm_lock runs a query on the lock connection, which opens a
    # transaction there. Left open, the session sits `idle in transaction` for
    # the rest of the run rather than `idle` -- and
    # idle_in_transaction_session_timeout is commonly set where
    # idle_session_timeout is not, so the check added to survive an idle timeout
    # would have made its own connection a better target for one.
    engine = create_async_engine(db_url)
    watcher = create_async_engine(db_url, isolation_level="AUTOCOMMIT")
    states: list[list[str]] = []
    real_confirm = migrations_module._confirm_lock

    async def sample(lock_conn, **kwargs):
        await real_confirm(lock_conn, **kwargs)
        # The lock connection is never touched by this probe: an earlier version
        # rolled it back to read its pid and so created what it was measuring.
        async with watcher.connect() as w:
            rows = (
                await w.execute(
                    sa.text(
                        "SELECT state FROM pg_stat_activity "
                        "WHERE datname = :n AND pid <> pg_backend_pid()"
                    ),
                    {"n": db_url.database},
                )
            ).all()
        states.append(sorted(r.state for r in rows))

    migrations_module._confirm_lock = sample
    try:
        await apply_pending(engine, migrations_dir, emit=lambda _: None)
    finally:
        migrations_module._confirm_lock = real_confirm
        await watcher.dispose()
        await engine.dispose()

    assert states, "the check never ran, so this asserts nothing"
    for observed in states:
        # Exactly one of each: the work connection is mid-migration and belongs
        # in a transaction; the lock connection does not.
        assert observed == ["idle", "idle in transaction"], observed


async def test_a_lock_already_held_by_the_pooled_session_is_refused(
    db_url: URL, migrations_dir: Path
):
    # Session advisory locks are REENTRANT. A session that already holds the key
    # lets pg_try_advisory_lock succeed and raises the hold count, while the one
    # release at the end lowers it by one — so the lock stays held on a
    # connection returned to the pool, blocking every other process while this
    # pool's next run takes it again and never notices. Measured: two acquires
    # and one release leave it held.
    engine = create_async_engine(db_url, pool_size=2, max_overflow=0)
    try:
        # Exactly ONE connection is left in the pool, holding the key. That is
        # what makes this deterministic rather than a bet on the pool's
        # hand-back order: the run's first checkout can only be this one, and
        # the first checkout is the lock connection. (Planting it on two is not
        # an option either way — advisory locks are exclusive between sessions,
        # so the second acquire simply returns false.)
        async with engine.connect() as conn:
            assert await conn.scalar(
                sa.text("SELECT pg_catalog.pg_try_advisory_lock(:k)"),
                {"k": migrations_module.ADVISORY_LOCK_KEY},
            )
            await conn.commit()

        with pytest.raises(MigrationLockError, match="already holds the migration advisory lock"):
            await apply_pending(engine, migrations_dir, emit=lambda _: None)
    finally:
        await engine.dispose()

    verify = create_async_engine(db_url)
    try:
        assert not await table_exists(verify, "widget")  # refused before anything ran
    finally:
        await verify.dispose()


async def test_the_lock_session_is_not_returned_to_the_pool_holding_anything(
    db_url: URL, migrations_dir: Path
):
    # The structural half of the above: the runner must never be what leaks a
    # level, so every exit path ENDS the lock session rather than releasing one
    # level and pooling it. That is why the check above can treat a held lock as
    # somebody else's problem rather than its own.
    #
    # Asserting "nothing holds the key afterwards" would NOT have tested this —
    # the single pg_advisory_unlock already zeroes the count on every path the
    # check allows, so that assertion passes with the discard removed. Measured;
    # it is the assertion this test started with. What the fix actually does is
    # end the session, so that is what is observed: the backend the lock ran on
    # must be gone, not idle in the pool.
    engine = create_async_engine(db_url, pool_size=2, max_overflow=0)
    watcher = create_async_engine(db_url, isolation_level="AUTOCOMMIT")
    lock_pids: list[int] = []
    real_lock = migrations_module._lock

    async def note_the_pid(conn):
        await real_lock(conn)
        lock_pids.append(await conn.scalar(sa.text("SELECT pg_catalog.pg_backend_pid()")))
        await conn.commit()

    migrations_module._lock = note_the_pid
    try:
        assert await apply_pending(engine, migrations_dir, emit=lambda _: None) == 2
    finally:
        migrations_module._lock = real_lock

    try:
        assert len(lock_pids) == 1, lock_pids  # or the spy never ran
        async with watcher.connect() as w:
            alive = await w.scalar(
                sa.text("SELECT count(*) FROM pg_stat_activity WHERE pid = :p"),
                {"p": lock_pids[0]},
            )
        assert alive == 0, "the lock session went back to the pool instead of ending"
    finally:
        await watcher.dispose()
        await engine.dispose()


async def test_a_migrations_directory_with_no_forward_files_is_refused(
    db_engine: AsyncEngine, tmp_path: Path
):
    # An artifact that lost its migrations would create an empty ledger, print
    # `applied 0 migration(s)` and exit 0 with no schema installed. Every other
    # "nothing to do" in this runner is backed by a history saying so.
    empty = tmp_path / "no_forward_files"
    empty.mkdir()
    with pytest.raises(MigrationError, match="no forward migrations found"):
        await apply_pending(db_engine, empty, emit=lambda _: None)

    # Orphaned rollback files are the same case, not a different one.
    (empty / "001_first.down.sql").write_text("DROP TABLE IF EXISTS widget;")
    with pytest.raises(MigrationError, match="no forward migrations found"):
        await apply_pending(db_engine, empty, emit=lambda _: None)

    # And it refused before creating the ledger it would have read as empty.
    assert not await table_exists(db_engine, "schema_migrations")


async def test_two_overlapping_runs_cannot_both_commit(db_url: URL, migrations_dir: Path):
    """Opposite directions touch DIFFERENT ledger rows, so per-row checks miss it.

    Measured before the fence, with a rollback of 002 and an apply of 003
    overlapping: both runs succeeded, the history was left reading `001, 003`,
    and 003's effects stood on a schema 002 had been unwound from — a state no
    sequence of files describes.

    The fence is two things and this exercises both, by releasing the two runs
    from a barrier so neither is serialised by the test itself:

    * the transaction-scoped writer lock — without it both re-read the same
      history at the same moment, both find it unchanged, and both commit;
    * the re-read — without it the loser merely waits its turn and then applies
      a plan built on a history that no longer exists.

    Which run wins is the pool's business, so this asserts the shape: exactly
    one succeeds, the other is refused naming the change, and what is left is a
    history the sequence can describe.
    """
    write_pair(migrations_dir, "003_third", "CREATE TABLE sprocket (id INT);", None)
    only_two = migrations_dir.parent / "only_two"
    only_two.mkdir()
    for name in ("001_first", "002_second"):
        for suffix in (".sql", ".down.sql"):
            (only_two / f"{name}{suffix}").write_text(
                (migrations_dir / f"{name}{suffix}").read_text()
            )

    setup = create_async_engine(db_url)
    try:
        await apply_pending(setup, only_two, emit=lambda _: None)  # 001, 002 applied
    finally:
        await setup.dispose()

    applier = create_async_engine(db_url)
    unwinder = create_async_engine(db_url)
    real_fence = migrations_module._fence
    real_lock, real_confirm = migrations_module._lock, migrations_module._confirm_lock
    both_ready = asyncio.Barrier(2)

    async def wait_for_the_other(conn, ledger, expected):
        # Both runs arrive having read the SAME history and are released
        # together, so nothing but the runner's own fence orders them.
        assert sorted(expected) == ["001_first.sql", "002_second.sql"], expected
        await both_ready.wait()
        return await real_fence(conn, ledger, expected)

    # The run lock is NEUTRALISED here, because a lock that has stopped
    # excluding is the finding's premise: its session can be closed after
    # _confirm_lock passed and a second run take the key while the first
    # transaction is still live. Leaving the real lock in place would let it
    # refuse one of these runs and the fence would never be reached — which is
    # exactly what the first draft of this test measured instead of the fence.
    async def no_exclusion(conn):
        return 0

    async def no_confirmation(lock_conn, *, backend=None):
        return None

    migrations_module._fence = wait_for_the_other
    migrations_module._lock = no_exclusion
    migrations_module._confirm_lock = no_confirmation
    try:
        outcomes = await asyncio.gather(
            apply_pending(applier, migrations_dir, emit=lambda _: None),
            downgrade_to(unwinder, only_two, "001_first.sql", emit=lambda _: None),
            return_exceptions=True,
        )
    finally:
        migrations_module._fence = real_fence
        migrations_module._lock = real_lock
        migrations_module._confirm_lock = real_confirm
        for engine in (applier, unwinder):
            await engine.dispose()

    refused = [o for o in outcomes if isinstance(o, MigrationError)]
    assert len(refused) == 1, f"expected exactly one refusal, got {outcomes}"
    assert "history changed while this run was working" in str(refused[0]), refused[0]
    assert len([o for o in outcomes if isinstance(o, int)]) == 1, outcomes

    verify = create_async_engine(db_url)
    try:
        history = await applied_names(verify)
        # Whichever won, what is left is describable: either 002 was unwound and
        # 003 never ran, or 003 was applied on top of an intact 001-002.
        assert history in (
            ["001_first.sql"],
            ["001_first.sql", "002_second.sql", "003_third.sql"],
        ), history
        assert await table_exists(verify, "sprocket") == ("003_third.sql" in history)
    finally:
        await verify.dispose()


async def test_a_lock_session_that_moves_to_another_backend_is_refused(
    db_url: URL, migrations_dir: Path
):
    # A session-scoped advisory lock belongs to one backend. Through a
    # transaction-pooling proxy (PgBouncer in transaction mode) the commit in
    # _lock hands the backend back, and a later statement can land on a
    # different one — so the confirmation would interrogate a session that never
    # took the key, and invalidating the client connection could not end the one
    # that did. I have no PgBouncer here, so this simulates the observable
    # property rather than the proxy: the backend under the lock connection is
    # not the one the key was taken on.
    engine = create_async_engine(db_url)
    real_lock = migrations_module._lock

    async def lock_then_move(conn):
        return await real_lock(conn) + 1  # as if the next statement landed elsewhere

    migrations_module._lock = lock_then_move
    try:
        with pytest.raises(MigrationError, match="the session is not stable"):
            await apply_pending(engine, migrations_dir, emit=lambda _: None)
    finally:
        migrations_module._lock = real_lock
        await engine.dispose()

    verify = create_async_engine(db_url)
    try:
        assert await applied_names(verify) == []  # refused before anything was recorded
    finally:
        await verify.dispose()


async def test_status_leaves_the_callers_search_path_alone(db_url: URL, migrations_dir: Path):
    # _ensure_bookkeeping runs RESET search_path and status committed it, on a
    # connection that then went back to the pool. RESTORED rather than
    # discarded: discarding ends the session, which loses the caller's setting
    # just as thoroughly — measured, both land on the database default.
    engine = create_async_engine(db_url, pool_size=1, max_overflow=0)
    try:
        async with engine.connect() as conn:
            await conn.execute(sa.text("CREATE SCHEMA tenant"))
            await conn.execute(sa.text("SET search_path = tenant, public"))
            await conn.commit()

        assert await status(engine, migrations_dir)  # and it still did its job

        async with engine.connect() as conn:
            assert await conn.scalar(sa.text("SHOW search_path")) == "tenant, public"
    finally:
        await engine.dispose()


async def test_a_cancellation_during_cleanup_is_not_swallowed(db_url: URL, migrations_dir: Path):
    # _discard suppressed BaseException around invalidate(), so a cancellation
    # landing there was consumed: the caller had cancelled the task and got a
    # completed migration count back instead. Measured: apply_pending returned 2.
    engine = create_async_engine(db_url)
    real_invalidate = AsyncConnection.invalidate

    async def cancel_in_cleanup(self, *args, **kwargs):
        raise asyncio.CancelledError()

    AsyncConnection.invalidate = cancel_in_cleanup  # type: ignore[method-assign]
    try:
        with pytest.raises(asyncio.CancelledError):
            await apply_pending(engine, migrations_dir, emit=lambda _: None)
    finally:
        AsyncConnection.invalidate = real_invalidate  # type: ignore[method-assign]
        await engine.dispose()
