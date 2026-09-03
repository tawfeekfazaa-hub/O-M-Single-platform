"""Numbered plain-SQL migration runner (docs/DECISIONS.md ADR-003, ADR-006).

Guarantees this runner provides, in the order they matter:

1. **What was applied can never change, and neither can its rollback.** Both the
   forward file and its ``.down.sql`` are checksummed when the migration is
   applied and re-verified before they are used again. A rollback file is as
   destructive as the forward file is constructive, so it gets the same
   protection.
2. **What is executed is what was checksummed.** Each file is read exactly once,
   and that content is both hashed and executed — a file replaced mid-run
   (a deploy updating a shared checkout) cannot record one hash and run another.
3. **History is a prefix.** Applied migrations must be the first N of the
   discovered sequence. A migration numbered behind ones already applied is
   refused, because applying it would make the recorded order a lie and send
   rollback down the wrong path.
4. **One writer at a time**, via a session-level advisory lock that is released
   explicitly rather than left on a pooled connection.
5. **Safe to re-run.** Applying twice is a no-op.
6. **Nothing is trusted silently.** A row from the pre-checksum runner is refused
   until an operator adopts it deliberately, and ``--status`` reports drift
   instead of hiding it.

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


class MigrationOrderError(MigrationError):
    """The applied migrations are not a prefix of the discovered sequence."""


class Emit(Protocol):
    def __call__(self, message: str) -> None: ...


def _normalize(raw: bytes) -> str:
    """Decode with newline normalization.

    A Windows checkout can rewrite LF to CRLF (the README documents a Windows
    workflow), which would change a byte-level hash without changing a single
    SQL statement and refuse every subsequent run. Normalizing makes the
    checksum a property of the SQL rather than of the checkout — and because
    the SAME normalized text is what gets executed, "checksummed" and
    "executed" cannot drift apart.
    """
    return raw.replace(b"\r\n", b"\n").decode("utf-8")


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Migration:
    filename: str
    path: Path
    #: Read ONCE at discovery; this exact text is hashed and executed.
    content: str
    checksum: str
    down_content: str | None
    down_checksum: str | None

    @property
    def has_down(self) -> bool:
        return self.down_content is not None

    @property
    def down_filename(self) -> str:
        return self.filename[: -len(".sql")] + DOWN_SUFFIX


@dataclass(frozen=True, slots=True)
class MigrationState:
    filename: str
    applied: bool
    has_down: bool
    #: None when consistent; otherwise why history and files disagree.
    drift: str | None = None


def read_migration(path: Path) -> Migration:
    content = _normalize(path.read_bytes())
    down_path = path.with_name(path.name[: -len(".sql")] + DOWN_SUFFIX)
    down_content = _normalize(down_path.read_bytes()) if down_path.is_file() else None
    return Migration(
        filename=path.name,
        path=path,
        content=content,
        checksum=_digest(content),
        down_content=down_content,
        down_checksum=_digest(down_content) if down_content is not None else None,
    )


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
        migrations.append(read_migration(path))
    return migrations


@dataclass(frozen=True, slots=True)
class _AppliedRow:
    checksum: str | None
    down_checksum: str | None


async def _ensure_bookkeeping(conn: AsyncConnection) -> None:
    """Create or upgrade ``schema_migrations``.

    The PR-1 runner tracked only (filename, applied_at); the ADD COLUMN
    statements upgrade an existing deployment in place.
    """
    await conn.execute(
        sa.text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  filename TEXT PRIMARY KEY,"
            "  checksum TEXT,"
            "  down_checksum TEXT,"
            "  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()"
            ")"
        )
    )
    for column in ("checksum", "down_checksum"):
        await conn.execute(
            sa.text(f"ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS {column} TEXT")
        )


async def _applied_rows(conn: AsyncConnection) -> dict[str, _AppliedRow]:
    result = await conn.execute(
        sa.text("SELECT filename, checksum, down_checksum FROM schema_migrations")
    )
    return {r.filename: _AppliedRow(r.checksum, r.down_checksum) for r in result}


def _require_prefix(migrations: list[Migration], applied: dict[str, _AppliedRow]) -> None:
    """Applied migrations must be the FIRST N of the discovered sequence.

    With 001 and 003 applied, a 002 arriving later would be applied *after* 003.
    The bookkeeping would then imply filename order while the real execution
    order was 001, 003, 002 — and rollback, which unwinds in reverse filename
    order, would run the down files in an order that never happened.
    """
    expected = [m.filename for m in migrations[: len(applied)]]
    if set(expected) != set(applied):
        out_of_order = sorted(set(expected) ^ set(applied))
        raise MigrationOrderError(
            "applied migrations are not a prefix of the migration sequence "
            f"(mismatch around: {', '.join(out_of_order)}) — a migration numbered "
            "behind ones already applied cannot be applied in place; renumber it "
            "to the end of the sequence"
        )


async def _verify(
    conn: AsyncConnection,
    applied: dict[str, _AppliedRow],
    known: dict[str, Migration],
    *,
    adopt_legacy: bool,
    emit: Emit,
) -> None:
    """Refuse the run if history and files disagree. Never trusts silently."""
    missing = sorted(name for name in applied if name not in known)
    if missing:
        raise MigrationChecksumError(
            "these migrations are recorded as applied but are no longer present: "
            + ", ".join(missing)
            + " — restore the files or repair schema_migrations by hand"
        )
    changed = sorted(
        name
        for name, row in applied.items()
        if row.checksum is not None and row.checksum != known[name].checksum
    )
    if changed:
        raise MigrationChecksumError(
            "these migrations were edited after being applied: "
            + ", ".join(changed)
            + " — an applied migration is immutable; add a new one instead"
        )

    legacy = sorted(name for name, row in applied.items() if row.checksum is None)
    if legacy and not adopt_legacy:
        # Recording the current file's hash would declare the database verified
        # against SQL that may never have run there — precisely the drift the
        # checksum exists to reveal. Adoption is an operator decision.
        raise MigrationChecksumError(
            "these migrations were applied by a runner that recorded no checksum: "
            + ", ".join(legacy)
            + " — their content cannot be verified. Confirm the database matches "
            "the current files, then re-run with --adopt-legacy-checksums"
        )
    for name in legacy:
        migration = known[name]
        await conn.execute(
            sa.text(
                "UPDATE schema_migrations SET checksum = :c, down_checksum = :d WHERE filename = :f"
            ),
            {"c": migration.checksum, "d": migration.down_checksum, "f": name},
        )
        emit(f"adopt {name}  (unverified baseline recorded on operator request)")


async def _run_sql(conn: AsyncConnection, sql: str, *, filename: str) -> None:
    """Execute one migration's SQL inside the caller's transaction.

    asyncpg prepares statements, which forbids multi-statement strings, so this
    goes to the raw driver connection — which also means SQLAlchemy's exception
    translation does not apply and a bad migration would otherwise raise a raw
    driver error straight past the runner's own taxonomy and out of the CLI as a
    traceback. The failure is re-raised as a MigrationError naming the FILE and
    the error type only: never the SQL, never a value, never connection details.

    The text comes from the Migration read at discovery and is never re-read.
    """
    raw = await conn.get_raw_connection()
    try:
        await raw.driver_connection.execute(sql)
    except Exception as exc:
        raise MigrationError(f"{filename} failed to execute: {type(exc).__name__}") from exc


async def _lock(conn: AsyncConnection) -> None:
    acquired = await conn.scalar(
        sa.text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY}
    )
    if not acquired:
        raise MigrationLockError(
            "another migration run holds the advisory lock; refusing to run concurrently"
        )
    # Session-scoped, so it survives this commit and every later per-migration
    # transaction on the same connection.
    await conn.commit()


async def _unlock(conn: AsyncConnection) -> None:
    """Release explicitly — closing the connection only returns it to the pool.

    A session-level lock left on a pooled connection stays held: another engine
    or process blocks on it, while a second call through the same pooled session
    succeeds anyway because advisory locks are reentrant, hiding the leak.
    """
    await conn.rollback()
    await conn.execute(sa.text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
    await conn.commit()


async def apply_pending(
    engine: AsyncEngine,
    directory: Path,
    *,
    emit: Emit = print,
    adopt_legacy: bool = False,
) -> int:
    """Apply every migration not yet recorded. Returns how many were applied."""
    migrations = discover(directory)
    known = {m.filename: m for m in migrations}
    applied_count = 0
    async with engine.connect() as conn:
        await _lock(conn)
        try:
            await _ensure_bookkeeping(conn)
            await conn.commit()
            applied = await _applied_rows(conn)
            await _verify(conn, applied, known, adopt_legacy=adopt_legacy, emit=emit)
            _require_prefix(migrations, applied)
            await conn.commit()

            for migration in migrations:
                if migration.filename in applied:
                    emit(f"skip  {migration.filename}")
                    continue
                async with conn.begin():
                    await _run_sql(conn, migration.content, filename=migration.filename)
                    await conn.execute(
                        sa.text(
                            "INSERT INTO schema_migrations (filename, checksum, down_checksum) "
                            "VALUES (:f, :c, :d)"
                        ),
                        {
                            "f": migration.filename,
                            "c": migration.checksum,
                            "d": migration.down_checksum,
                        },
                    )
                emit(f"apply {migration.filename}")
                applied_count += 1
        finally:
            await _unlock(conn)
    return applied_count


async def downgrade_to(
    engine: AsyncEngine,
    directory: Path,
    target: str,
    *,
    emit: Emit = print,
    adopt_legacy: bool = False,
) -> int:
    """Unwind every migration applied AFTER ``target``. Returns how many.

    ``target`` is a migration filename that stays applied, or ``base`` to unwind
    everything. Nothing is unwound unless every down file needed is present AND
    matches the checksum recorded when its migration was applied — a rollback
    file edited afterwards can drop far more than its own migration created.
    """
    migrations = discover(directory)
    known = {m.filename: m for m in migrations}
    if target != BASE_TARGET and target not in known:
        raise MigrationError(f"unknown --down-to target: {target}")

    async with engine.connect() as conn:
        await _lock(conn)
        try:
            await _ensure_bookkeeping(conn)
            await conn.commit()
            applied = await _applied_rows(conn)
            await _verify(conn, applied, known, adopt_legacy=adopt_legacy, emit=emit)
            _require_prefix(migrations, applied)
            await conn.commit()

            # Reverse filename order, which _require_prefix has just proven is
            # also the reverse of the real application order.
            doomed = [m for m in reversed(migrations) if m.filename in applied]
            if target != BASE_TARGET:
                doomed = [m for m in doomed if m.filename > target]

            # Everything is checked for the WHOLE set before anything runs:
            # discovering a bad rollback half-way would leave the schema in a
            # state no file describes.
            without_down = [m.filename for m in doomed if not m.has_down]
            if without_down:
                raise MigrationError(
                    "cannot roll back — these migrations have no .down.sql: "
                    + ", ".join(sorted(without_down))
                )
            tampered = sorted(
                m.filename
                for m in doomed
                if applied[m.filename].down_checksum is not None
                and applied[m.filename].down_checksum != m.down_checksum
            )
            if tampered:
                raise MigrationChecksumError(
                    "these rollback files changed after their migration was applied: "
                    + ", ".join(tampered)
                    + " — a rollback is destructive, so it is not run unless it is the "
                    "one that accompanied the applied migration"
                )
            unverifiable = sorted(
                m.filename for m in doomed if applied[m.filename].down_checksum is None
            )
            for name in unverifiable:
                # The down file did not exist when the migration was applied, so
                # there is nothing to compare it against. Say so rather than
                # implying it was verified.
                emit(f"warn  {name}  (rollback file was not recorded at apply time)")

            for migration in doomed:
                async with conn.begin():
                    assert migration.down_content is not None  # checked above
                    await _run_sql(conn, migration.down_content, filename=migration.down_filename)
                    await conn.execute(
                        sa.text("DELETE FROM schema_migrations WHERE filename = :f"),
                        {"f": migration.filename},
                    )
                emit(f"down  {migration.filename}")
        finally:
            await _unlock(conn)
    return len(doomed)


async def status(engine: AsyncEngine, directory: Path) -> list[MigrationState]:
    """Applied/pending state per migration, including any history drift.

    Reports names and drift only, never file contents. This is the command an
    operator reaches for when something looks wrong, so it must SHOW the drift
    that apply and rollback refuse to run with — a diagnostic that hides the
    fault is worse than none.
    """
    migrations = discover(directory)
    known = {m.filename: m for m in migrations}
    async with engine.connect() as conn:
        await _ensure_bookkeeping(conn)
        await conn.commit()
        applied = await _applied_rows(conn)

    states: list[MigrationState] = []
    for migration in migrations:
        row = applied.get(migration.filename)
        drift: str | None = None
        if row is not None:
            if row.checksum is None:
                drift = "applied without a checksum (unverifiable)"
            elif row.checksum != migration.checksum:
                drift = "file edited after it was applied"
            elif (
                row.down_checksum is not None
                and migration.down_checksum is not None
                and row.down_checksum != migration.down_checksum
            ):
                drift = "rollback file edited after it was applied"
        states.append(
            MigrationState(migration.filename, row is not None, migration.has_down, drift)
        )

    # Applied rows whose file is gone would otherwise vanish from the report —
    # the one drift an operator is least likely to notice on their own.
    for name in sorted(set(applied) - set(known)):
        states.append(MigrationState(name, True, False, "applied but the file is missing"))
    return states
