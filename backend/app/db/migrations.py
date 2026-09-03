"""Numbered plain-SQL migration runner (docs/DECISIONS.md ADR-003, ADR-006).

Guarantees this runner provides, in the order they matter:

1. **An applied migration can never change.** Every file's checksum is stored
   when it is applied and verified on every later run; an edit to already-applied
   SQL refuses the whole run instead of silently diverging from what production
   actually has.
2. **One writer at a time.** A session-level advisory lock makes two concurrent
   runners impossible, so a half-applied file cannot interleave with another
   process's.
3. **Safe to re-run.** Applying twice is a no-op; the second run reports "skip".
4. **A tested way back.** Every migration ships a paired ``.down.sql`` and
   ``--down-to`` unwinds in reverse order, refusing to start unless every down
   file it needs is present.

The SQL files remain the source of truth for DDL; ``app/db/tables.py`` mirrors
them for query building only.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

#: Forward migrations are ``NNN_name.sql``; their rollback is ``NNN_name.down.sql``.
MIGRATION_PATTERN = re.compile(r"^\d{3}_[a-z0-9_]+\.sql$")
DOWN_SUFFIX = ".down.sql"

#: ``--down-to base`` unwinds every applied migration.
BASE_TARGET = "base"

# A fixed 64-bit key so every runner competes for the SAME advisory lock. Derived
# from a constant string rather than typed as a magic number, so the derivation is
# auditable and cannot drift between callers.
ADVISORY_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"aq_om.schema_migrations").digest()[:8], "big", signed=True
)


class MigrationError(RuntimeError):
    """A migration cannot be applied or rolled back safely."""


class MigrationChecksumError(MigrationError):
    """An already-applied migration no longer matches what was applied."""


class MigrationLockError(MigrationError):
    """Another migration run holds the advisory lock."""


class Emit(Protocol):
    def __call__(self, message: str) -> None: ...


@dataclass(frozen=True, slots=True)
class Migration:
    filename: str
    path: Path
    checksum: str

    @property
    def down_path(self) -> Path:
        return self.path.with_name(self.filename[: -len(".sql")] + DOWN_SUFFIX)

    @property
    def has_down(self) -> bool:
        return self.down_path.is_file()


@dataclass(frozen=True, slots=True)
class MigrationState:
    filename: str
    applied: bool
    has_down: bool


def checksum_of(path: Path) -> str:
    """SHA-256 of the migration's content, newline-normalized.

    A Windows checkout can rewrite LF to CRLF (README documents a Windows
    workflow), which would change a byte-level hash without changing a single
    SQL statement and refuse every subsequent run. Normalizing keeps the
    checksum a property of the SQL, not of the checkout.
    """
    raw = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(raw).hexdigest()


def discover(directory: Path) -> list[Migration]:
    """Forward migrations in filename order, rejecting anything unorderable."""
    if not directory.is_dir():
        raise MigrationError(f"migrations directory not found: {directory}")
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        if path.name.endswith(DOWN_SUFFIX):
            continue
        if not MIGRATION_PATTERN.match(path.name):
            # Ordering is the whole contract of a numbered runner; a file that
            # cannot be ordered deterministically must not be guessed at.
            raise MigrationError(f"migration filename does not match NNN_name.sql: {path.name}")
        migrations.append(Migration(path.name, path, checksum_of(path)))
    return migrations


async def _ensure_bookkeeping(conn: AsyncConnection) -> None:
    """Create or upgrade ``schema_migrations``.

    The PR-1 runner tracked only (filename, applied_at); adding the column with
    IF NOT EXISTS upgrades an existing deployment in place.
    """
    await conn.execute(
        sa.text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  filename TEXT PRIMARY KEY,"
            "  checksum TEXT,"
            "  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()"
            ")"
        )
    )
    await conn.execute(
        sa.text("ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum TEXT")
    )


async def _applied_rows(conn: AsyncConnection) -> dict[str, str | None]:
    result = await conn.execute(sa.text("SELECT filename, checksum FROM schema_migrations"))
    return {row.filename: row.checksum for row in result}


async def _verify_and_adopt(
    conn: AsyncConnection,
    applied: dict[str, str | None],
    known: dict[str, Migration],
    emit: Emit,
) -> None:
    """Refuse the run if history and files disagree; adopt pre-checksum rows.

    Three distinct situations, deliberately not collapsed into one message:
    a recorded file that no longer exists, a recorded file whose content
    changed, and a row written by the pre-checksum runner.
    """
    missing = sorted(name for name in applied if name not in known)
    if missing:
        raise MigrationChecksumError(
            "these migrations are recorded as applied but are no longer present: "
            + ", ".join(missing)
            + " — restore the files or repair schema_migrations by hand"
        )
    changed = sorted(
        name
        for name, recorded in applied.items()
        if recorded is not None and recorded != known[name].checksum
    )
    if changed:
        raise MigrationChecksumError(
            "these migrations were edited after being applied: "
            + ", ".join(changed)
            + " — an applied migration is immutable; add a new one instead"
        )
    for name, recorded in sorted(applied.items()):
        if recorded is None:
            # Written before checksums existed: the original content is unknown,
            # so the current file is adopted as the baseline and the adoption is
            # announced rather than done silently.
            await conn.execute(
                sa.text("UPDATE schema_migrations SET checksum = :c WHERE filename = :f"),
                {"c": known[name].checksum, "f": name},
            )
            emit(f"adopt {name}  (checksum recorded for a pre-checksum row)")


async def _run_sql_file(conn: AsyncConnection, path: Path) -> None:
    # asyncpg prepares statements, which forbids multi-statement strings — run
    # migration files on the raw driver connection, inside the caller's transaction.
    raw = await conn.get_raw_connection()
    await raw.driver_connection.execute(path.read_text())


async def _lock(conn: AsyncConnection) -> None:
    acquired = await conn.scalar(
        sa.text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY}
    )
    if not acquired:
        raise MigrationLockError(
            "another migration run holds the advisory lock; refusing to run concurrently"
        )
    # The lock is session-scoped, so it survives this commit and every later
    # per-migration transaction on the same connection.
    await conn.commit()


async def apply_pending(engine: AsyncEngine, directory: Path, *, emit: Emit = print) -> int:
    """Apply every migration not yet recorded. Returns how many were applied."""
    migrations = discover(directory)
    known = {m.filename: m for m in migrations}
    applied_count = 0
    async with engine.connect() as conn:
        await _lock(conn)
        await _ensure_bookkeeping(conn)
        await conn.commit()
        applied = await _applied_rows(conn)
        await _verify_and_adopt(conn, applied, known, emit)
        await conn.commit()

        for migration in migrations:
            if migration.filename in applied:
                emit(f"skip  {migration.filename}")
                continue
            async with conn.begin():
                await _run_sql_file(conn, migration.path)
                await conn.execute(
                    sa.text("INSERT INTO schema_migrations (filename, checksum) VALUES (:f, :c)"),
                    {"f": migration.filename, "c": migration.checksum},
                )
            emit(f"apply {migration.filename}")
            applied_count += 1
    return applied_count


async def downgrade_to(
    engine: AsyncEngine, directory: Path, target: str, *, emit: Emit = print
) -> int:
    """Unwind every migration applied AFTER ``target``. Returns how many.

    ``target`` is a migration filename that stays applied, or ``base`` to unwind
    everything. Nothing is unwound unless every down file needed is present, so
    a missing rollback file can never leave the schema half-way.
    """
    migrations = discover(directory)
    known = {m.filename: m for m in migrations}
    if target != BASE_TARGET and target not in known:
        raise MigrationError(f"unknown --down-to target: {target}")

    async with engine.connect() as conn:
        await _lock(conn)
        await _ensure_bookkeeping(conn)
        await conn.commit()
        applied = await _applied_rows(conn)
        await _verify_and_adopt(conn, applied, known, emit)
        await conn.commit()

        # Reverse filename order: the newest applied migration is undone first.
        doomed = [m for m in reversed(migrations) if m.filename in applied]
        if target != BASE_TARGET:
            doomed = [m for m in doomed if m.filename > target]

        without_down = [m.filename for m in doomed if not m.has_down]
        if without_down:
            raise MigrationError(
                "cannot roll back — these migrations have no .down.sql: "
                + ", ".join(sorted(without_down))
            )

        for migration in doomed:
            async with conn.begin():
                await _run_sql_file(conn, migration.down_path)
                await conn.execute(
                    sa.text("DELETE FROM schema_migrations WHERE filename = :f"),
                    {"f": migration.filename},
                )
            emit(f"down  {migration.filename}")
    return len(doomed)


async def status(engine: AsyncEngine, directory: Path) -> list[MigrationState]:
    """Applied/pending state per migration — names only, never file contents."""
    migrations = discover(directory)
    async with engine.connect() as conn:
        await _ensure_bookkeeping(conn)
        await conn.commit()
        applied = await _applied_rows(conn)
    return [MigrationState(m.filename, m.filename in applied, m.has_down) for m in migrations]
