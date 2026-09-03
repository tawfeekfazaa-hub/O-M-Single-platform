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
6. **A migration and its history row commit together.** Each migration runs in
   one transaction with its ``schema_migrations`` write, so the schema can never
   advance without its history. A file that manages its own transaction would
   break that pair, so it is refused before it runs and the transaction is
   measured again after it does.
7. **Nothing is trusted silently.** A row from the pre-checksum runner is refused
   until an operator adopts it deliberately, and ``--status`` reports drift
   instead of hiding it. Every refusal has a documented way forward that does not
   involve editing ``schema_migrations`` by hand.

The SQL files remain the source of truth for DDL; ``app/db/tables.py`` mirrors
them for query building only.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
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


#: Statements that end or re-scope the transaction the runner opened. A file
#: containing one commits itself, so its bookkeeping row can no longer fail with
#: it — the atomicity guarantee below would silently stop holding.
_TX_CONTROL = frozenset({"BEGIN", "COMMIT", "END", "ROLLBACK", "ABORT", "SAVEPOINT", "RELEASE"})
#: Only transaction control when followed by TRANSACTION; ``PREPARE stmt AS`` is not.
_TX_CONTROL_PAIRS = frozenset({("START", "TRANSACTION"), ("PREPARE", "TRANSACTION")})
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")
#: A PostgreSQL 14+ ``LANGUAGE SQL`` body: ``BEGIN ATOMIC ... END``, not a
#: transaction. Unlike a ``$$ ... $$`` body it is not quoted, so it cannot be
#: blanked out wholesale.
_BEGIN_ATOMIC = re.compile(r"\bBEGIN\s+ATOMIC\b", re.IGNORECASE)
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _ident_char(ch: str) -> bool:
    """Whether ``ch`` can continue an unquoted PostgreSQL identifier.

    Deliberately liberal: treating a character as part of an identifier can
    only make the scanner blank LESS, which risks a false alarm. Treating one
    as a delimiter blanks more, which risks missing a real COMMIT.
    """
    return ch.isalnum() or ch in "_$"


def _blank_quoted_and_commented(sql: str) -> str:
    """Replace comments and quoted text with spaces, keeping every other offset.

    Keyword scanning is only honest once the parts of the file that merely
    *contain* words are neutralised: a ``COMMIT`` in a comment, in a string
    literal, or inside a PL/pgSQL ``$$ BEGIN ... END $$`` body is not
    transaction control, and refusing a migration over one would be a false
    alarm an author could not work around.
    """
    out = list(sql)
    n = len(sql)
    i = 0

    def blank(start: int, stop: int) -> None:
        for k in range(start, stop):
            if out[k] != "\n":  # keep line numbers intact for any future reporting
                out[k] = " "

    def scan_quote(start: int, quote: str, backslash_escapes: bool) -> int:
        j = start + 1
        while j < n:
            if backslash_escapes and sql[j] == "\\" and j + 1 < n:
                j += 2
                continue
            if sql[j] == quote:
                if j + 1 < n and sql[j + 1] == quote:  # '' or "" is an escaped quote
                    j += 2
                    continue
                return j + 1
            j += 1
        return n  # unterminated: the database will reject it, we just stop here

    while i < n:
        ch = sql[i]
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end == -1 else end
            blank(i, end)
            i = end
        elif sql.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):  # PostgreSQL block comments nest
                    depth += 1
                    j += 2
                elif sql.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            blank(i, j)
            i = j
        elif ch in "'\"":
            # E'...' honours backslash escapes; a plain '...' does not.
            escaped = (
                ch == "'"
                and i > 0
                and sql[i - 1] in "Ee"
                and (i == 1 or not _WORD.match(sql[i - 2]))
            )
            end = scan_quote(i, ch, escaped)
            blank(i, end)
            i = end
        elif (
            ch == "$"
            and not _ident_char(sql[i - 1] if i else "")
            and (tag := _DOLLAR_TAG.match(sql, i))
        ):
            # A `$` that CONTINUES an identifier does not open a dollar quote:
            # PostgreSQL accepts `foo$$` as a table name, and reading its `$$`
            # as an opener blanks everything up to the next `$$` in the file —
            # hiding any real COMMIT in between. Closing is deliberately not
            # boundary-checked: PostgreSQL ends a dollar-quoted string at the
            # first literal occurrence of the tag, wherever it falls.
            close = sql.find(tag.group(0), tag.end())
            end = n if close == -1 else close + len(tag.group(0))
            blank(i, end)
            i = end
        else:
            i += 1
    return "".join(out)


def _transaction_control(sql: str) -> list[str]:
    """Transaction-control statements found in ``sql``, in order of appearance.

    Best effort by design: it is the *guard*, not the guarantee. The guarantee
    is the post-execution check in :func:`_run_sql`, which measures whether the
    transaction is still open rather than inferring it from the text.
    """
    found: list[str] = []
    open_bodies = 0
    for statement in _blank_quoted_and_commented(sql).split(";"):
        words = _WORD.findall(statement)
        if not words:
            continue
        first = words[0].upper()
        second = words[1].upper() if len(words) > 1 else ""

        if first == "END" and open_bodies:
            # This END closes a BEGIN ATOMIC body rather than a transaction.
            # Statements INSIDE such a body never begin with END — a
            # `CASE ... END` sits mid-statement — so the first statement-initial
            # END after the body opened is precisely its terminator. Counting
            # them keeps the exemption to the body: a second END, with no body
            # left open, is a COMMIT synonym and is still refused.
            open_bodies -= 1
            continue
        if first in _TX_CONTROL:
            found.append(first)
        elif (first, second) in _TX_CONTROL_PAIRS:
            found.append(f"{first} {second}")
        open_bodies += len(_BEGIN_ATOMIC.findall(statement))
    return found


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


def _reject_transaction_control(content: str, filename: str) -> None:
    statements = _transaction_control(content)
    if statements:
        raise MigrationError(
            f"{filename} manages its own transaction ("
            + ", ".join(sorted(set(statements)))
            + ") — the runner already wraps each migration in one, together with "
            "its schema_migrations row, and a migration that commits itself "
            "breaks that pair: the schema would advance with no history. Remove "
            "the transaction-control statements; the file is a migration, not a "
            "standalone script."
        )


def read_migration(path: Path) -> Migration:
    content = _normalize(path.read_bytes())
    _reject_transaction_control(content, path.name)
    down_path = path.with_name(path.name[: -len(".sql")] + DOWN_SUFFIX)
    down_content = _normalize(down_path.read_bytes()) if down_path.is_file() else None
    if down_content is not None:
        _reject_transaction_control(down_content, down_path.name)
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

    down_changed = sorted(
        name
        for name, row in applied.items()
        if row.down_checksum is not None and row.down_checksum != known[name].down_checksum
    )
    if down_changed:
        # Checked on EVERY run, not only when rolling back: letting the schema
        # advance while its recovery path is known to be corrupt is exactly the
        # moment a rollback is most likely to be needed.
        raise MigrationChecksumError(
            "these rollback files were edited or removed after their migration was "
            "applied: "
            + ", ".join(down_changed)
            + " — an applied migration's recovery path is part of its history"
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


async def _adopt_new_rollbacks(
    conn: AsyncConnection,
    applied: dict[str, _AppliedRow],
    known: dict[str, Migration],
    *,
    emit: Emit,
) -> None:
    """Record a rollback file that was written after its migration was applied.

    Its recorded ``down_checksum`` is NULL, which is evidence the file did not
    accompany the migration, so ``downgrade_to`` refuses to execute it and
    ``status`` reports drift. Without this there was no way to vouch for such a
    file while KEEPING the migration applied: the deployment gate stayed at exit
    2 permanently, and the only remedy the runner offered was to roll the
    migration back — destroying data to clear a bookkeeping gap.

    Adoption is what it says: it authorises this exact text to run as that
    migration's rollback later, on nothing but an operator's word. Hence the
    same explicit flag as an unverifiable forward checksum, and the same
    reporting.
    """
    for name in sorted(
        name
        for name, row in applied.items()
        if row.checksum is not None
        and row.down_checksum is None
        and known[name].down_checksum is not None
    ):
        await conn.execute(
            sa.text("UPDATE schema_migrations SET down_checksum = :d WHERE filename = :f"),
            {"d": known[name].down_checksum, "f": name},
        )
        applied[name] = _AppliedRow(applied[name].checksum, known[name].down_checksum)
        emit(f"adopt {name}  (rollback file recorded unverified on operator request)")


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
    # This one statement does two load-bearing jobs, which is why it is not the
    # no-op it resembles:
    #
    # 1. It opens the transaction. SQLAlchemy's is lazy — conn.begin() creates
    #    the transaction OBJECT, but asyncpg opens the real one only when a
    #    statement goes through the adapter. Reaching for the driver first would
    #    run the migration in its own implicit transaction, committing
    #    independently of the bookkeeping row that follows — measured: the DDL
    #    survived a rollback that should have discarded it.
    # 2. It pins how the migration text is lexed. With
    #    standard_conforming_strings OFF (settable per database or per role),
    #    PostgreSQL honours backslash escapes in ordinary literals, so
    #    'it\'s' is ONE string — while the guard in read_migration() reads it as
    #    two and blanks whatever follows, which could include a real COMMIT.
    #    Forcing it on makes the guard's reading and the server's the same.
    #    SET LOCAL, so it lasts exactly as long as this migration's transaction.
    try:
        await conn.execute(sa.text("SET LOCAL standard_conforming_strings = on"))
        driver = (await conn.get_raw_connection()).driver_connection
    except Exception as exc:
        # Outside the block below, this would escape the taxonomy: a connection
        # lost between taking the lock and starting the migration is an ordinary
        # disconnect, and the CLI catches only MigrationError.
        raise MigrationError(f"{filename} could not be started: {type(exc).__name__}") from exc

    try:
        await driver.execute(sql)
    except Exception as exc:
        raise MigrationError(f"{filename} failed to execute: {type(exc).__name__}") from exc

    # The guarantee behind the guard in read_migration(). A file that ends the
    # transaction — a COMMIT the scanner did not recognise, a procedure that
    # commits internally — would leave the statements above already durable
    # while the bookkeeping row that follows could still fail, which is exactly
    # the schema-without-history state this pairing exists to prevent. Measured
    # from the driver rather than inferred from the text, so it holds however
    # the transaction was ended.
    if not driver.is_in_transaction():
        raise MigrationError(
            f"{filename} ended the runner's transaction before its "
            "schema_migrations row could be written — whatever it did is now "
            "committed with NO history, so the next run would apply it again. "
            "Remove any transaction control from the file, then reconcile "
            "schema_migrations with the schema by hand"
        )


@asynccontextmanager
async def _atomic(conn: AsyncConnection) -> AsyncIterator[None]:
    """One migration and its history row, committed or discarded together.

    ``async with conn.begin()`` does that too, but it rolls back on the way out
    of a failing body — and when the failure IS the database going away, that
    rollback raises as well and REPLACES the migration error, which the CLI then
    cannot recognise as a refusal. Measured: a migration that loses its backend
    surfaced as ``InterfaceError: cannot call Transaction.rollback(): the
    underlying connection is closed``, with the real reason buried.

    The original failure is the one worth keeping. A connection that died has
    already had its transaction discarded by the server, so there is nothing the
    failed rollback would have achieved.
    """
    tx = await conn.begin()
    try:
        yield
    except BaseException:
        with suppress(Exception):
            await tx.rollback()
        raise
    else:
        await tx.commit()


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


async def _discard(conn: AsyncConnection) -> None:
    """End this connection's session, best effort, however broken it is.

    Ending the session is what actually releases a session-scoped advisory
    lock, so this must not depend on the connection still working — and under
    cancellation even the async path can be interrupted again, which is why the
    synchronous fallback exists.
    """
    with suppress(BaseException):
        await conn.invalidate()
        return
    with suppress(BaseException):  # pragma: no cover - only reachable mid-cancel
        conn.sync_connection.invalidate()  # type: ignore[union-attr]


async def _unlock(conn: AsyncConnection, *, emit: Emit) -> None:
    """Release explicitly — closing the connection only returns it to the pool.

    A session-level lock left on a pooled connection stays held: another engine
    or process blocks on it, while a second call through the same pooled session
    succeeds anyway because advisory locks are reentrant, hiding the leak.

    This runs in a ``finally``, so anything it raises would REPLACE the failure
    that brought us here — a database that went away mid-migration would reach
    the operator as a closed-connection traceback instead of the refusal the CLI
    knows how to report. So a cleanup failure never propagates: the connection is
    discarded instead, which ends its session and takes the session-scoped lock
    with it, and the reason is reported alongside rather than in place of the
    real one.
    """
    try:
        await conn.rollback()
        await conn.execute(sa.text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
        await conn.commit()
    except BaseException as exc:
        # BaseException, not Exception: asyncio.CancelledError inherits straight
        # from BaseException, so a caller cancelling mid-cleanup used to skip the
        # handler entirely and return a still-locked session to the pool — where
        # the same pool's next call succeeds reentrantly while every other
        # process blocks. Measured: another engine could not take the lock.
        await _discard(conn)
        if not isinstance(exc, Exception):
            raise  # cancellation is the caller's to see; the lock is gone now
        emit(
            f"note: the advisory lock could not be released cleanly ({type(exc).__name__}); "
            "the connection was discarded, which ends its session and the lock with it"
        )


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
            if adopt_legacy:
                await _adopt_new_rollbacks(conn, applied, known, emit=emit)
            await conn.commit()

            for migration in migrations:
                if migration.filename in applied:
                    emit(f"skip  {migration.filename}")
                    continue
                async with _atomic(conn):
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
            await _unlock(conn, emit=emit)
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
            # An edited or removed rollback file is already refused by _verify,
            # for every applied migration rather than only the doomed ones.
            unverifiable = sorted(
                m.filename for m in doomed if applied[m.filename].down_checksum is None
            )
            if unverifiable and not adopt_legacy:
                # A recorded NULL is evidence this file did NOT accompany the
                # applied migration. It can contain any destructive statement,
                # which is the very risk the rollback checksum exists to remove,
                # so a warning is not authorization — an operator has to say so.
                raise MigrationChecksumError(
                    "these migrations were applied with no rollback file, and one has "
                    "since appeared: "
                    + ", ".join(unverifiable)
                    + " — it did not accompany the applied migration and cannot be "
                    "verified. Review it, then re-run with --adopt-legacy-checksums"
                )
            for name in unverifiable:
                emit(f"adopt {name}  (unverified rollback executed on operator request)")

            for migration in doomed:
                async with _atomic(conn):
                    assert migration.down_content is not None  # checked above
                    await _run_sql(conn, migration.down_content, filename=migration.down_filename)
                    await conn.execute(
                        sa.text("DELETE FROM schema_migrations WHERE filename = :f"),
                        {"f": migration.filename},
                    )
                emit(f"down  {migration.filename}")
        finally:
            await _unlock(conn, emit=emit)
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

    # Ordering is a property of the SEQUENCE, not of one file, so it is computed
    # once and attributed to the files that break it. Without this, the
    # branch-merge state that apply and rollback both refuse would report clean
    # and exit 0 — useless as a deployment gate.
    out_of_order: set[str] = set()
    expected = {m.filename for m in migrations[: len(applied)]}
    if expected != set(applied):
        out_of_order = expected ^ set(applied)

    states: list[MigrationState] = []
    for migration in migrations:
        row = applied.get(migration.filename)
        drift: str | None = None
        if row is not None:
            if row.checksum is None:
                drift = "applied without a checksum (unverifiable)"
            elif row.checksum != migration.checksum:
                drift = "file edited after it was applied"
            elif (row.down_checksum is None) != (migration.down_checksum is None):
                # Presence changed either way: a recorded rollback that is now
                # gone (recovery path lost) or one that appeared afterwards
                # (never verified). Comparing only non-null values missed both.
                drift = (
                    "rollback file removed after it was applied"
                    if row.down_checksum is not None
                    else "rollback file added after it was applied (unverified)"
                )
            elif row.down_checksum != migration.down_checksum:
                drift = "rollback file edited after it was applied"
        if drift is None and migration.filename in out_of_order:
            drift = "applied out of sequence (history is not a prefix)"
        states.append(
            MigrationState(migration.filename, row is not None, migration.has_down, drift)
        )

    # An applied row whose file is gone would otherwise vanish from the report —
    # the one drift an operator is least likely to notice unaided. It also lands
    # in out_of_order by construction, but "the file is missing" is the more
    # specific and more actionable of the two, so it is the one reported.
    for name in sorted(set(applied) - set(known)):
        states.append(MigrationState(name, True, False, "applied but the file is missing"))
    return states
