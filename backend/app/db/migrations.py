"""Numbered plain-SQL migration runner (docs/DECISIONS.md ADR-003, ADR-006).

Guarantees this runner provides, in the order they matter:

1. **What was applied can never change, and neither can its rollback.** Both the
   forward file and its ``.down.sql`` are checksummed when the migration is
   applied and re-verified before they are used again. A rollback file is as
   destructive as the forward file is constructive, so it gets the same
   protection.
2. **What is executed is what was checksummed.** Each file is read exactly once,
   and that exact text is both hashed and executed — no normalization anywhere,
   so two files that would execute differently can never share a checksum. A
   file replaced mid-run (a deploy updating a shared checkout) cannot record one
   hash and run another. Line endings are pinned by ``.gitattributes`` instead,
   which is checked by a test.
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
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Protocol

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

#: Forward migrations are ``NNN_name.sql``; their rollback is ``NNN_name.down.sql``.
#: ``[0-9]`` rather than ``\d``: Python's ``\d`` also matches Unicode decimal
#: digits, so ``٠٠٢_beta.sql`` would pass and then compare unequal to ``002``,
#: slipping a reused sequence number past the duplicate check below.
MIGRATION_PATTERN = re.compile(r"^[0-9]{3}_[a-z0-9_]+\.sql$")
DOWN_SUFFIX = ".down.sql"

#: ``--down-to base`` unwinds every applied migration.
BASE_TARGET = "base"

# A fixed 64-bit key so every runner competes for the SAME advisory lock. Derived
# from a constant string rather than typed as a magic number, so the derivation is
# auditable and cannot drift between callers.
ADVISORY_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"aq_om.schema_migrations").digest()[:8], "big", signed=True
)

# A SECOND key, held for the length of each writer transaction rather than the
# run. The run lock lives on a session that can be closed under it (see
# _confirm_lock); this one is transaction-scoped, so it cannot outlive or
# predecease the work it fences, and `pg_advisory_unlock_all()` cannot release
# it — transaction locks are released only by ending the transaction. Derived
# from its own string so it can never collide with the run lock, which the work
# connection must not contend with.
WRITER_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"aq_om.schema_migrations.writer").digest()[:8], "big", signed=True
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


def _reporting(emit: Emit) -> Emit:
    """Wrap a caller's ``emit`` so a failure to report cannot change the result.

    ``emit`` is supplied by the caller: a closed stdout, a logging handler that
    raises, a test callback. Every announcement in this module is made AFTER the
    work it describes has committed, so letting the reporter's exception
    propagate turned a completed run into ``migration refused`` and exit 2 —
    telling automation to handle a failure that did not happen, for a state
    change that did. Measured: a ``BrokenPipeError`` from ``emit`` left the
    table created and its history row written, and the CLI reported a refusal.

    Exceptions only. A cancellation passing through the reporter is still the
    caller's to see.
    """

    def report(message: str) -> None:
        with suppress(Exception):
            emit(message)

    return report


def _decode(raw: bytes) -> str:
    """The file's own text, unchanged. This is what gets executed."""
    return raw.decode("utf-8")


def _checksum(content: str) -> str:
    """SHA-256 over the exact text that will be executed. No normalization.

    This was briefly a hash of newline-normalized content, so that a CRLF
    checkout could not change every checksum and refuse every run. That folding
    is gone, because it made two files that EXECUTE DIFFERENTLY hash the same:
    a physical CRLF inside a string literal is part of the value, so an LF and a
    CRLF checkout inserted different data under one recorded history, and
    editing an applied literal from LF to CRLF passed the immutability check.

    The checkout problem is handled where it belongs — ``.gitattributes`` pins
    ``*.sql`` to LF in the working tree, and a test asserts no migration in this
    repository contains a CR — rather than by making the checksum blind to a
    difference that reaches the database.

    So "what was checksummed is what runs" is an identity again, not an
    argument: same text, same bytes, one read.
    """
    return _digest(content)


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


#: Statements that end or re-scope the transaction the runner opened. A file
#: containing one commits itself, so its bookkeeping row can no longer fail with
#: it — the atomicity guarantee below would silently stop holding.
_TX_CONTROL = frozenset({"BEGIN", "COMMIT", "END", "ROLLBACK", "ABORT", "SAVEPOINT", "RELEASE"})
#: Only transaction control when followed by TRANSACTION; ``PREPARE stmt AS`` is not.
_TX_CONTROL_PAIRS = frozenset({("START", "TRANSACTION"), ("PREPARE", "TRANSACTION")})


def _is_ident_start(ch: str) -> bool:
    """PostgreSQL: a letter, an underscore, or any non-ASCII character."""
    return ch.isalpha() or ch == "_" or ord(ch) >= 128


def _is_ident_cont(ch: str) -> bool:
    """As above, plus digits and dollar signs after the first character."""
    return ch.isalnum() or ch in "_$" or ord(ch) >= 128


def _string_end(sql: str, start: int, quote: str, *, backslash_escapes: bool) -> int:
    """Index just past the string or quoted identifier opening at ``start``."""
    n = len(sql)
    i = start + 1
    while i < n:
        if backslash_escapes and sql[i] == "\\" and i + 1 < n:
            i += 2
            continue
        if sql[i] == quote:
            if i + 1 < n and sql[i + 1] == quote:  # '' and "" are escaped quotes
                i += 2
                continue
            return i + 1
        i += 1
    return n  # unterminated: the server will reject it, we just stop here


def _dollar_delimiter(sql: str, start: int) -> int:
    """Length of the dollar-quote delimiter at ``start``, or 0 if it is not one.

    The tag follows identifier rules but cannot itself contain a dollar sign,
    so ``$$``, ``$body$`` and ``$é$`` are delimiters while ``$1`` is not.
    """
    n = len(sql)
    i = start + 1
    if i < n and _is_ident_start(sql[i]):
        i += 1
        while i < n and sql[i] != "$" and _is_ident_cont(sql[i]):
            i += 1
    return i - start + 1 if i < n and sql[i] == "$" else 0


class _Token(NamedTuple):
    word: str
    #: Parenthesis nesting where the token appears. A routine's parameters sit
    #: at depth 1, its SQL-standard body at depth 0 — which is the difference
    #: between `CREATE FUNCTION f(begin atomic)` and a real body opener.
    depth: int
    #: Whether only whitespace and comments separate this token from the one
    #: before it. `begin.atomic` is a qualified NAME, not the two keywords:
    #: the dot makes them two tokens that are adjacent in the word list but not
    #: in the SQL.
    adjacent: bool


def _statements(sql: str) -> list[list[_Token]]:
    """``sql`` split on ``;``, each statement as its upper-cased word tokens.

    A tokenizer rather than a search, because every bypass found in review came
    from reading a keyword, a quote delimiter or a string prefix out of the
    MIDDLE of an identifier: ``foo$$``, ``foo$BEGIN ATOMIC``, ``foo$E'...'``.
    PostgreSQL lexes an identifier greedily and ``$`` is an identifier character
    after the first, so consuming whole identifiers is what makes those
    misreadings impossible, rather than excluding them one at a time.

    Comments, string literals and quoted identifiers yield no tokens: they can
    contain any words at all without meaning them.
    """
    statements: list[list[_Token]] = []
    words: list[_Token] = []
    depth = 0
    separated = False  # punctuation seen since the last word token
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if sql.startswith("--", i):
            # PostgreSQL ends a line comment at CR as well as LF, and a bare CR
            # is not folded anywhere (the checksum normalizes, the text does
            # not). Measured: with
            # `-- note<CR>CREATE TABLE ...`, the server ran the statement after
            # the comment. Looking only for LF would read it as commented out.
            breaks = [p for p in (sql.find("\n", i), sql.find("\r", i)) if p != -1]
            i = min(breaks) if breaks else n
        elif sql.startswith("/*", i):
            # `nesting`, NOT `depth`: this counter belongs to the comment, and
            # reusing the parenthesis one left it at zero afterwards — so a
            # block comment anywhere inside a routine signature made every
            # token after it look top-level.
            nesting, i = 1, i + 2
            while i < n and nesting:  # PostgreSQL block comments nest
                if sql.startswith("/*", i):
                    nesting, i = nesting + 1, i + 2
                elif sql.startswith("*/", i):
                    nesting, i = nesting - 1, i + 2
                else:
                    i += 1
        elif ch == ";":
            statements.append(words)
            words = []
            depth = 0  # a new statement starts outside any parentheses
            separated = False
            i += 1
        elif ch in "'\"":
            # Plain literals never honour backslash escapes, because the runner
            # executes migrations with standard_conforming_strings = on.
            i = _string_end(sql, i, ch, backslash_escapes=False)
            separated = True
        elif ch == "$" and (length := _dollar_delimiter(sql, i)):
            tag = sql[i : i + length]
            # PostgreSQL closes at the first literal occurrence of the tag,
            # wherever it falls, so the close is deliberately not boundary-checked.
            close = sql.find(tag, i + length)
            i = n if close == -1 else close + length
            separated = True
        elif _is_ident_start(ch):
            end = i + 1
            while end < n and _is_ident_cont(sql[end]):
                end += 1
            word = sql[i:end]
            if word in ("E", "e") and end < n and sql[end] == "'":
                # E'...' is one lexical unit and the only string form where a
                # backslash escapes. `foo$E'...'` is NOT one — that E belongs to
                # the identifier — which is why this asks about the whole token
                # rather than the character before the quote.
                i = _string_end(sql, end, "'", backslash_escapes=True)
                separated = True
            else:
                words.append(_Token(word.upper(), depth, not separated))
                separated = False
                i = end
        elif ch.isspace():
            i += 1  # whitespace does not separate two keywords
        else:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            separated = True
            i += 1  # numbers, operators, punctuation, $1 parameters
    statements.append(words)
    return statements


def _defines_a_routine(words: list[str]) -> bool:
    """Whether ``words`` begin a ``CREATE [OR REPLACE] FUNCTION|PROCEDURE``.

    A ``BEGIN ATOMIC`` body can only belong to one of those, so nothing else
    can open one — and the two words land side by side in ordinary SQL often
    enough to matter: ``CREATE TABLE begin (atomic int)`` is a table with a
    column, ``SELECT * FROM begin atomic`` a table with an alias. Both used to
    open a phantom body and spend the exemption on a real ``END``.
    """
    if words[:1] != ["CREATE"]:
        return False
    rest = words[1:]
    if rest[:2] == ["OR", "REPLACE"]:
        rest = rest[2:]
    return rest[:1] in (["FUNCTION"], ["PROCEDURE"])


def _transaction_control(sql: str) -> list[str]:
    """Transaction-control statements found in ``sql``, in order of appearance.

    A guard, not the guarantee. The guarantee is the post-execution check in
    :func:`_run_sql`, which measures whether the transaction is still open
    rather than inferring it from the text.
    """
    found: list[str] = []
    open_bodies = 0
    for tokens in _statements(sql):
        if not tokens:
            continue
        words = [t.word for t in tokens]
        first = words[0]
        second = words[1] if len(words) > 1 else ""

        if first == "END" and open_bodies:
            # Closes a BEGIN ATOMIC body rather than a transaction. Statements
            # inside such a body never begin with END — a `CASE ... END` sits
            # mid-statement — so the first statement-initial END after a body
            # opens is precisely its terminator.
            open_bodies -= 1
            continue
        if first in _TX_CONTROL:
            found.append(first)
        elif (first, second) in _TX_CONTROL_PAIRS:
            found.append(f"{first} {second}")

        if _defines_a_routine(words):
            # AT MOST ONE, because one statement defines at most one routine and
            # therefore has at most one body. Summing every matching pair counted
            # the real opener AND a table/alias pair inside the body's first
            # statement — `BEGIN ATOMIC SELECT x FROM begin atomic` — and then
            # exempted two ENDs, the second of which commits the transaction.
            #
            # The first qualifying pair is the opener: a routine's options come
            # before its body, and its parameters are inside parentheses, so
            # nothing at depth 0 can precede it.
            open_bodies += any(
                a.word == "BEGIN" and b.word == "ATOMIC" and a.depth == 0 and b.adjacent
                for a, b in zip(tokens, tokens[1:], strict=False)
            )
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
    # Read once. That single text is executed, scanned, and — after newline
    # normalization — hashed, so no two of those can be looking at different
    # bytes.
    content = _decode(path.read_bytes())
    _reject_transaction_control(content, path.name)
    down_path = path.with_name(path.name[: -len(".sql")] + DOWN_SUFFIX)
    down_content = _decode(down_path.read_bytes()) if down_path.is_file() else None
    if down_content is not None:
        _reject_transaction_control(down_content, down_path.name)
    return Migration(
        filename=path.name,
        path=path,
        content=content,
        checksum=_checksum(content),
        down_content=down_content,
        down_checksum=_checksum(down_content) if down_content is not None else None,
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

    # Two files numbered the same are the branch-merge case the prefix rule is
    # supposed to catch, but filename order hides it: `002_beta` sorts after
    # `002_alpha`, so the applied set stays a prefix and a second `002` is
    # applied. The recorded revision is then ambiguous — "we are at 002" names
    # two different schemas.
    seen: dict[str, str] = {}
    for migration in migrations:
        number = migration.filename[:3]
        if number in seen:
            raise MigrationOrderError(
                f"two migrations are numbered {number}: {seen[number]} and "
                f"{migration.filename} — renumber one to the end of the sequence"
            )
        seen[number] = migration.filename

    if not migrations:
        # A deploy whose artifact lost its migrations would otherwise create an
        # empty ledger, print `applied 0 migration(s)` and exit 0 with no schema
        # installed — reporting success for a database the application cannot
        # use. Every other "nothing to do" here is backed by a history saying so;
        # this one is backed by nothing at all.
        raise MigrationError(
            f"no forward migrations found in {directory} — the directory exists but holds "
            "none (an incomplete deployment artifact, or only .down.sql files). Installing "
            "no schema and reporting success is not a state this can report as a no-op"
        )
    return migrations


@dataclass(frozen=True, slots=True)
class _AppliedRow:
    checksum: str | None
    down_checksum: str | None


async def _ledger_locations(conn: AsyncConnection) -> tuple[str | None, list[str]]:
    """Where ``schema_migrations`` is: the one this search_path resolves, and the rest.

    Both names come back ``format('%I.%I', ...)``-quoted by the server rather
    than assembled here.

    The resolution query is deliberately operator-free — one qualified function
    call and a literal — because it must run under the REAL search_path to
    resolve the same name the rest of the session would. Everything after it is
    pinned to ``pg_catalog`` by :func:`_ledger`, for the reason recorded there:
    this is the connection migration SQL shares, so ``=`` itself is shadowable.
    """
    # _ledger pins the path for the rest of this TRANSACTION, and the caller
    # still has a `CREATE TABLE schema_migrations` to run in it — which under
    # the pin tried to create `pg_catalog.schema_migrations` and was refused by
    # the server. So restore what was in force on the way in.
    entry_path = str(await conn.scalar(sa.text("SHOW search_path")))
    oid = await conn.scalar(
        sa.text("SELECT pg_catalog.to_regclass('schema_migrations')::pg_catalog.oid")
    )
    rows = (
        await _ledger(
            conn,
            sa.text(
                "SELECT c.oid, pg_catalog.format('%I.%I', n.nspname, c.relname) AS name, "
                "c.relpersistence OPERATOR(pg_catalog.=) 't' AS temporary "
                "FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relname = 'schema_migrations' "
                # Only a real table can be the ledger: the runner creates one.
                # Anything else answering to the name — a view put there to
                # shadow it — is not it, and leaving it out is what makes the
                # refusal below fire instead of writing history into it.
                "AND c.relkind = ANY (ARRAY['r', 'p'])"
            ),
        )
    ).all()
    await conn.execute(
        sa.text("SELECT pg_catalog.set_config('search_path', :p, true)"), {"p": entry_path}
    )

    # A TEMPORARY table is a real table with relkind 'r', and PostgreSQL searches
    # the session's implicit pg_temp schema BEFORE search_path for relation
    # names — after RESET too. So a temporary `schema_migrations` on this
    # connection is what to_regclass answers with, and it would be accepted as
    # the ledger. Measured on a session carrying one: an empty history, every
    # migration re-applied, a data migration's row inserted twice, exit 0. Then
    # _restore_session ends the session and the "history" goes with it, leaving
    # the schema changes committed and unrecorded.
    #
    # Refused rather than skipped, the same way a ledger off the path is: the
    # runner's own statements are all qualified and would have been safe, but
    # something put a ledger-shaped table in front of the real one and that is
    # not a state to continue through quietly.
    shadow = next((r for r in rows if oid is not None and r.oid == oid and r.temporary), None)
    if shadow is not None:
        raise MigrationError(
            f"a TEMPORARY table named schema_migrations ({shadow.name}) is shadowing the "
            "ledger on this connection. PostgreSQL resolves relation names in the session's "
            "temporary schema first, so it would be read as the history and every migration "
            "re-applied. Nothing has been applied. Drop it, or run on a connection without it"
        )

    real = [r for r in rows if not r.temporary]
    on_path = next((r.name for r in real if oid is not None and r.oid == oid), None)
    return on_path, sorted(r.name for r in real if r.name != on_path)


async def _ensure_bookkeeping(conn: AsyncConnection) -> str:
    """Create or upgrade ``schema_migrations``; return its qualified name.

    The PR-1 runner tracked only (filename, applied_at); the ADD COLUMN
    statements upgrade an existing deployment in place.

    The RETURN VALUE is the security-relevant part. Migration SQL runs on this
    same connection and may legitimately ``SET search_path``, which would then
    resolve a later unqualified ``schema_migrations`` somewhere else entirely.
    Measured: a migration that created ``app.schema_migrations`` and set the
    search path wrote its history row there, leaving the real ledger empty — so
    the next run saw the migration as unapplied and would apply it again.
    Resolving the name ONCE here, before any migration has run, and qualifying
    every later ledger statement with it, removes the redirection rather than
    forbidding the SET.
    """
    # Start from the session default rather than whatever is currently set. A
    # migration's `SET search_path` persists on the connection, and the
    # connection goes back to the pool, so WITHOUT this the next run resolves
    # the ledger against the previous run's leftover path — found by the test
    # for the qualification below, which re-applied an applied migration.
    await conn.execute(sa.text("RESET search_path"))
    ledger, elsewhere = await _ledger_locations(conn)

    if ledger is None and elsewhere:
        # RESET restores the CONFIGURED default, which is not beyond a
        # migration's reach: `ALTER DATABASE ... SET search_path = evil, public`
        # outlives the session, the pool and the process. Measured on a fresh
        # engine afterwards: RESET yielded `evil, public`, `CREATE TABLE IF NOT
        # EXISTS schema_migrations` made a SECOND, empty ledger there, and the
        # run re-applied an already-applied migration — silently, inserting a
        # data migration's row a second time. An operator retargeting `ALTER
        # ROLE ... SET search_path` gets there by accident just as easily.
        #
        # Creating a second ledger is never the right answer: an empty history
        # means "nothing has ever been applied". Refusing only in this exact
        # transition leaves a database that legitimately hosts several
        # applications, each with its own ledger in its own schema, working —
        # each one's search_path finds its own, and this branch never runs.
        raise MigrationError(
            "schema_migrations is not on this connection's search_path, but "
            f"{'one already exists' if len(elsewhere) == 1 else 'ledgers already exist'} at "
            f"{', '.join(elsewhere)}. Creating another here would read as an empty history "
            "and re-apply every migration, so nothing has been applied. Point search_path at "
            "the existing ledger, or move it, and run again"
        )

    if ledger is None:
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
        ledger, _ = await _ledger_locations(conn)
        if not ledger:  # pragma: no cover - it was just created
            raise MigrationError("schema_migrations could not be resolved after creating it")

    for column in ("checksum", "down_checksum"):
        # Qualified, like every other ledger statement: unqualified, this
        # upgraded whatever the current path happened to resolve.
        await _ledger(conn, sa.text(f"ALTER TABLE {ledger} ADD COLUMN IF NOT EXISTS {column} TEXT"))
    return ledger


async def _ledger(conn: AsyncConnection, statement: sa.TextClause, params: dict | None = None):
    """Run a bookkeeping statement with name resolution pinned to ``pg_catalog``.

    Qualifying the ledger RELATION is not enough. Migration SQL shares this
    session and can put its own schema ahead of ``pg_catalog``, which also
    redirects OPERATORS: measured, a down migration defining
    ``shadow.=(text, text)`` that returns false left the rolled-back migration
    still recorded as applied, because ``WHERE filename = :f`` matched nothing.
    The mirror image — an ``=`` returning true — would have deleted the whole
    history.

    ``SET LOCAL``, so it lasts only to the end of the current transaction. The
    bookkeeping statement is the last thing in it, and a migration's own
    session-level ``SET search_path`` survives for the migrations that follow.
    """
    await conn.execute(sa.text("SET LOCAL search_path = pg_catalog"))
    return await conn.execute(statement, params or {})


async def _applied_rows(conn: AsyncConnection, ledger: str) -> dict[str, _AppliedRow]:
    result = await _ledger(conn, sa.text(f"SELECT filename, checksum, down_checksum FROM {ledger}"))
    return {r.filename: _AppliedRow(r.checksum, r.down_checksum) for r in result}


async def _fence(conn: AsyncConnection, ledger: str, expected: set[str]) -> None:
    """Serialise this writer against every other, and re-check what it assumed.

    Two runs can both believe they hold the run lock: the lock session can be
    closed after :func:`_confirm_lock` passed but before this transaction
    commits. Checking the row this transaction is about to write is not enough,
    because two runs going in OPPOSITE directions touch DIFFERENT rows and so
    never collide. Measured, with a rollback of 002 and an apply of 003
    overlapping: both runs succeeded, and the history was left reading
    ``001, 003`` with 003's effects applied on a schema 002 had been unwound
    from — a state no sequence of files describes.

    Two halves, both needed:

    * The transaction-scoped lock makes writers take turns. Without it both can
      read the same history, act on it, and commit changes that do not conflict
      at the row level.
    * Re-reading the history inside the fence is what makes taking turns worth
      anything: the loser then sees that the history is no longer what its plan
      was built on. Without this the loser simply waits and then applies a stale
      plan.

    ``expected`` is what this run last saw. Anything else means somebody else
    committed to the ledger, so this run's remaining plan is not trustworthy —
    whichever direction either run was going.
    """
    # _ledger pins search_path to pg_catalog for the rest of the TRANSACTION, and
    # the migration runs later in this same one — under the pin its `CREATE
    # TABLE` tried to create in pg_catalog and was refused for lack of
    # privilege. So restore what was in force on the way in, exactly as ledger
    # discovery does.
    entry_path = str(await conn.scalar(sa.text("SHOW search_path")))
    await _ledger(
        conn, sa.text("SELECT pg_catalog.pg_advisory_xact_lock(:k)"), {"k": WRITER_LOCK_KEY}
    )
    rows = await _ledger(conn, sa.text(f"SELECT filename FROM {ledger}"))
    current = {r.filename for r in rows}
    await conn.execute(
        sa.text("SELECT pg_catalog.set_config('search_path', :p, true)"), {"p": entry_path}
    )
    if current != expected:
        appeared = sorted(current - expected)
        vanished = sorted(expected - current)
        raise MigrationError(
            "the migration history changed while this run was working"
            + (f" (now recorded: {', '.join(appeared)})" if appeared else "")
            + (f" (no longer recorded: {', '.join(vanished)})" if vanished else "")
            + " — another run committed to it, so two runs overlapped. This run's work has "
            "been rolled back and nothing further is applied. Check that only one migration "
            "run is started per deploy, then re-run"
        )


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
    ledger: str,
    *,
    adopt_legacy: bool,
) -> list[str]:
    """Refuse the run if history and files disagree. Never trusts silently.

    Returns what should be ANNOUNCED if the surrounding transaction commits —
    it does not emit. An adoption written here can still be rolled back by a
    later check (a history that is not a prefix) or by a failed commit, and
    saying "recorded" for a row that is still NULL sends an operator, or a log
    parser, away believing the opposite of the truth.
    """
    announcements: list[str] = []
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
        await _ledger(
            conn,
            sa.text(f"UPDATE {ledger} SET checksum = :c, down_checksum = :d WHERE filename = :f"),
            {"c": migration.checksum, "d": migration.down_checksum, "f": name},
        )
        announcements.append(f"adopt {name}  (unverified baseline recorded on operator request)")
    return announcements


async def _adopt_new_rollbacks(
    conn: AsyncConnection,
    applied: dict[str, _AppliedRow],
    known: dict[str, Migration],
    ledger: str,
) -> list[str]:
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

    Returns what to announce once the transaction commits, for the reason given
    on :func:`_verify`.
    """
    announcements: list[str] = []
    for name in sorted(
        name
        for name, row in applied.items()
        if row.checksum is not None
        and row.down_checksum is None
        and known[name].down_checksum is not None
    ):
        await _ledger(
            conn,
            sa.text(f"UPDATE {ledger} SET down_checksum = :d WHERE filename = :f"),
            {"d": known[name].down_checksum, "f": name},
        )
        applied[name] = _AppliedRow(applied[name].checksum, known[name].down_checksum)
        announcements.append(
            f"adopt {name}  (rollback file recorded unverified on operator request)"
        )
    return announcements


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


# The advisory key as PostgreSQL stores it. A bigint key is split across
# pg_locks.classid (high 32 bits) and pg_locks.objid (low 32), with objsubid 1.
# Split here rather than reassembled in SQL: this key is negative, and
# `classid::bigint * 4294967296 + objid` overflows bigint before it can match.
_LOCK_CLASSID = (ADVISORY_LOCK_KEY & 0xFFFFFFFFFFFFFFFF) >> 32
_LOCK_OBJID = ADVISORY_LOCK_KEY & 0xFFFFFFFF


async def _lock(conn: AsyncConnection) -> int:
    # Session advisory locks are REENTRANT: taking one this session already
    # holds succeeds and raises the hold count, while the single release below
    # lowers it by one — leaving the lock held on a connection that goes back to
    # the pool, where every other process blocks on it forever and this pool's
    # own next run takes it again reentrantly and never notices. Measured: two
    # acquires and one release leave it held.
    #
    # The release path ends this session (see _unlock), so the runner cannot be
    # what leaked it. Anything else holding this key on a checked-out session is
    # a state to refuse rather than build on.
    already = await conn.scalar(
        sa.text(
            "SELECT count(*) FROM pg_catalog.pg_locks WHERE locktype = 'advisory' "
            "AND pid = pg_catalog.pg_backend_pid() AND objsubid = 1 "
            "AND classid = :c AND objid = :o"
        ),
        {"c": _LOCK_CLASSID, "o": _LOCK_OBJID},
    )
    if already:
        raise MigrationLockError(
            "this connection's session already holds the migration advisory lock before the "
            "run started. Taking it again would nest, and releasing once would leave it held "
            "for every other process. Refusing on an engine whose pool is in that state"
        )
    acquired = await conn.scalar(
        sa.text("SELECT pg_catalog.pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY}
    )
    if not acquired:
        raise MigrationLockError(
            "another migration run holds the advisory lock; refusing to run concurrently"
        )
    # Session-scoped, so it survives this commit and every later per-migration
    # transaction on the same connection — provided the connection keeps the
    # same session across it, which _confirm_lock is given this pid to check.
    backend = await conn.scalar(sa.text("SELECT pg_catalog.pg_backend_pid()"))
    await conn.commit()
    return int(backend)


async def _discard(conn: AsyncConnection) -> None:
    """End this connection's session, best effort, however broken it is.

    Ending the session is what actually releases a session-scoped advisory
    lock, so this must not depend on the connection still working — and under
    cancellation even the async path can be interrupted again, which is why the
    synchronous fallback exists.

    A cancellation arriving DURING the cleanup is still the caller's, though.
    Swallowing it here let a cancelled ``apply_pending`` run to completion and
    return a successful count — measured, with ``invalidate()`` raising
    ``CancelledError``: the call returned 2. The fallback still runs (the lock
    has to go), and then it is re-raised.
    """
    cancellation: BaseException | None = None
    try:
        await conn.invalidate()
        return
    except Exception:
        pass  # a broken connection is the expected case; fall through
    except BaseException as exc:  # cancellation, or anything else not an Exception
        cancellation = exc
    with suppress(BaseException):  # pragma: no cover - only reachable mid-cancel
        conn.sync_connection.invalidate()  # type: ignore[union-attr]
    if cancellation is not None:
        raise cancellation


async def _two_connections(
    engine: AsyncEngine, stack: AsyncExitStack
) -> tuple[AsyncConnection, AsyncConnection]:
    """One connection for the lock, one for the work.

    The lock needs a session migration SQL cannot reach, which means the pool
    must be able to supply two. An engine configured ``pool_size=1,
    max_overflow=0`` otherwise blocks on the second checkout until
    ``pool_timeout`` and reports a pool error — a requirement the caller never
    agreed to, stated as one here instead.
    """
    lock_conn = await stack.enter_async_context(engine.connect())
    try:
        conn = await stack.enter_async_context(engine.connect())
    except Exception as exc:
        raise MigrationError(
            "could not open a second connection "
            f"({type(exc).__name__}): the run lock is held on a connection of its own, so "
            "migrations cannot run on an engine whose pool supplies fewer than two"
        ) from exc
    return lock_conn, conn


async def _confirm_lock(lock_conn: AsyncConnection, *, backend: int | None = None) -> None:
    """Fail the run if the lock session no longer holds the lock.

    The lock lives on its own connection, which is idle for as long as the
    migration takes — so ``idle_session_timeout`` or a proxy can close it, and a
    session-scoped advisory lock dies with its session. Measured: with the lock
    session terminated mid-migration, a second runner took the key and the first
    carried on regardless.

    Checked immediately before each history row is written, which is the last
    moment the work can still be abandoned. It doubles as a keep-alive: between
    migrations the lock session is no longer idle.
    """
    try:
        row = (
            await lock_conn.execute(
                sa.text(
                    "SELECT count(*) AS held, pg_catalog.pg_backend_pid() AS backend "
                    "FROM pg_catalog.pg_locks WHERE locktype = 'advisory' "
                    "AND pid = pg_catalog.pg_backend_pid()"
                )
            )
        ).one()
        held = row.held
        if backend is not None and row.backend != backend:
            # The lock is SESSION-scoped, so it belongs to a particular backend.
            # Through a transaction-pooling proxy (PgBouncer in transaction
            # mode) the commit in _lock returns the backend to the proxy and a
            # later statement may land on a different one — so this check would
            # be interrogating a session that never took the key, invalidating
            # the connection could not end the session that did, and the key
            # could be stranded against every future deploy. Nothing here can
            # make a pooled proxy safe; what it can do is refuse to pretend the
            # client connection is a PostgreSQL session.
            raise MigrationError(
                f"the run lock was taken on backend {backend} but this connection is now "
                f"backend {row.backend}: the session is not stable, which is what a "
                "transaction-pooling proxy (PgBouncer in transaction mode) does. A session "
                "advisory lock cannot fence a run across it. Point DATABASE_URL at the "
                "server directly, or at a session-pooled port. Nothing further is applied"
            )
    except MigrationError:
        raise
    except Exception as exc:
        raise MigrationError(
            f"lost contact with the session holding the run lock ({type(exc).__name__}); "
            "refusing to continue while another run may have taken it"
        ) from exc
    if not held:
        raise MigrationError(
            "the run lock is no longer held — its session was closed (an idle timeout or a "
            "proxy), so another run may already have started. Nothing further is applied"
        )
    # End the transaction this check opened. Without it the lock session sits
    # `idle in transaction` for the rest of the run instead of `idle`, which is
    # the state operators kill hardest — idle_in_transaction_session_timeout is
    # commonly set where idle_session_timeout is not. The check added to survive
    # an idle timeout would have made the connection a better target for one.
    # A session-level advisory lock is unaffected by the rollback.
    with suppress(Exception):
        await lock_conn.rollback()


async def _restore_session(conn: AsyncConnection) -> None:
    """Drop the connection a migration ran on, rather than pooling it.

    A migration's ``SET search_path`` is committed with it and rides back into
    the pool, so a library caller's next query — which never reaches
    :func:`_ensure_bookkeeping` and its reset — resolves unqualified names in
    the migration's schema. Measured: ``SHOW search_path`` returned ``app``, and
    ``statement_timeout`` ``1234ms``, on the next application query.

    ``RESET search_path`` fixed only the case that was found: a migration can
    also leave ``statement_timeout``, ``SET ROLE``, ``LISTEN`` registrations and
    temporary objects behind. ``DISCARD ALL`` covers all of those and is the
    obvious answer — but it runs ``DEALLOCATE ALL``, and asyncpg keeps a
    per-connection cache of prepared statements that the server has now
    forgotten, so the next caller to reuse that pooled connection gets
    ``prepared statement "__asyncpg_stmt_1d__" does not exist``. Measured: 17 of
    this module's own tests failed that way.

    So the session ends instead. Ending it discards every kind of state at once
    — including states nobody has thought of yet — and costs one reconnect on a
    connection that is used once per run. The pool simply opens a fresh one.
    """
    await _discard(conn)


async def _unlock(conn: AsyncConnection, *, emit: Emit) -> None:
    """Release explicitly — closing the connection only returns it to the pool.

    A session-level lock left on a pooled connection stays held: another engine
    or process blocks on it, while a second call through the same pooled session
    succeeds anyway because advisory locks are reentrant, hiding the leak.

    Runs even when the lock was never granted — a concurrent run holds it, or a
    cancellation landed on the grant so nobody knows. ``pg_advisory_unlock``
    only ever releases a lock held by THIS session, so releasing one we may not
    hold cannot disturb the run that does; it returns false and says so in the
    server log.

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
        await conn.execute(
            sa.text("SELECT pg_catalog.pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY}
        )
        await conn.commit()
        # Then end the session anyway, exactly as the failure path does. The
        # release above lowers the hold count by one, which is right only if the
        # count was one — and the check in _lock is what establishes that for
        # THIS run, not for a session shared with anything else. Ending it
        # leaves nothing behind to be reasoned about, at the cost of one
        # reconnect on a connection used once per run; the work connection is
        # already dropped for the same kind of reason.
        await _discard(conn)
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
    emit = _reporting(emit)
    migrations = discover(directory)
    known = {m.filename: m for m in migrations}
    applied_count = 0
    # The lock lives on its own connection. Migration SQL runs on the other one
    # and shares that session: a migration containing `SELECT
    # pg_advisory_unlock_all()` released the runner's own lock without ending
    # its transaction, and a second runner could then enter mid-migration —
    # measured. A separate session is not reachable from migration text at all.
    async with AsyncExitStack() as stack:
        lock_conn, conn = await _two_connections(engine, stack)
        # _lock is INSIDE the guarded region: PostgreSQL may grant the lock and
        # the caller be cancelled before the result is seen, which would return
        # a locked session to the pool with nothing arranged to release it.
        try:
            lock_pid = await _lock(lock_conn)
            ledger = await _ensure_bookkeeping(conn)
            await conn.commit()
            applied = await _applied_rows(conn, ledger)
            expected = set(applied)  # what every writer transaction re-checks
            announcements = await _verify(conn, applied, known, ledger, adopt_legacy=adopt_legacy)
            _require_prefix(migrations, applied)
            if adopt_legacy:
                announcements += await _adopt_new_rollbacks(conn, applied, known, ledger)
            await conn.commit()
            for line in announcements:  # only now is any of it true
                emit(line)

            for migration in migrations:
                if migration.filename in applied:
                    emit(f"skip  {migration.filename}")
                    continue
                async with _atomic(conn):
                    # Before the SQL, not after: the loser of an overlap must not
                    # execute the migration at all, and holding the fence for the
                    # duration is what makes "one writer" true rather than
                    # "one recorder".
                    await _fence(conn, ledger, expected)
                    await _run_sql(conn, migration.content, filename=migration.filename)
                    await _confirm_lock(lock_conn, backend=lock_pid)
                    try:
                        await _ledger(
                            conn,
                            sa.text(
                                f"INSERT INTO {ledger} (filename, checksum, down_checksum) "
                                "VALUES (:f, :c, :d)"
                            ),
                            {
                                "f": migration.filename,
                                "c": migration.checksum,
                                "d": migration.down_checksum,
                            },
                        )
                    except IntegrityError as exc:
                        # The lock is confirmed just above, but the window
                        # between that check and this transaction's COMMIT is
                        # not zero: the lock session can be closed inside it and
                        # another runner take the key while this work is still
                        # uncommitted. `filename` is the ledger's primary key, so
                        # a second runner that got there first turns this INSERT
                        # into a conflict rather than a duplicate row — and
                        # raising here discards THIS migration's SQL with it,
                        # which is the point: the work was applied twice, and one
                        # of the two must not survive.
                        raise MigrationError(
                            f"{migration.filename} was recorded by another run while this one "
                            "was applying it, so two runs overlapped. This run's copy of the "
                            "work has been rolled back and nothing further is applied; the "
                            "other run's stands. Check that only one migration run is started "
                            "per deploy, then re-run"
                        ) from exc
                expected.add(migration.filename)
                emit(f"apply {migration.filename}")
                applied_count += 1
        finally:
            # Nested, not sequential: _restore_session suppresses Exceptions but
            # a cancellation delivered inside it would otherwise skip the unlock
            # entirely and strand the lock — which is what the cancellation test
            # caught when these were two statements in a row.
            try:
                await _restore_session(conn)
            finally:
                await _unlock(lock_conn, emit=emit)
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
    emit = _reporting(emit)
    migrations = discover(directory)
    known = {m.filename: m for m in migrations}
    if target != BASE_TARGET and target not in known:
        raise MigrationError(f"unknown --down-to target: {target}")

    # As in apply_pending, on its own connection: migration SQL cannot reach a
    # session it does not run on.
    async with AsyncExitStack() as stack:
        lock_conn, conn = await _two_connections(engine, stack)
        # The lock is taken inside the region that releases it, so a
        # cancellation landing on the grant cannot strand it.
        try:
            lock_pid = await _lock(lock_conn)
            ledger = await _ensure_bookkeeping(conn)
            await conn.commit()
            applied = await _applied_rows(conn, ledger)
            expected = set(applied)  # what every writer transaction re-checks
            announcements = await _verify(conn, applied, known, ledger, adopt_legacy=adopt_legacy)
            _require_prefix(migrations, applied)
            await conn.commit()
            for line in announcements:  # only now is any of it true
                emit(line)

            if target != BASE_TARGET and target not in applied:
                # The file exists but the database never reached it. Filtering
                # would quietly produce an empty set, and "rolled back 0
                # migration(s)" with exit 0 tells automation it is now at a
                # revision it never was.
                raise MigrationError(
                    f"cannot roll back to {target}: it is not applied. The database is at "
                    + (max(applied) if applied else "base")
                )

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
            adopted = set(unverifiable)

            for migration in doomed:
                async with _atomic(conn):
                    assert migration.down_content is not None  # checked above
                    await _fence(conn, ledger, expected)
                    await _run_sql(conn, migration.down_content, filename=migration.down_filename)
                    await _confirm_lock(lock_conn, backend=lock_pid)
                    removed = await _ledger(
                        conn,
                        sa.text(f"DELETE FROM {ledger} WHERE filename = :f"),
                        {"f": migration.filename},
                    )
                    # The mirror of the INSERT conflict above, and the more
                    # dangerous half: there is no unique constraint to trip, so
                    # nothing here noticed. Measured — with the lock session
                    # closed after _confirm_lock passed, a second runner read the
                    # row as still applied (this transaction had not committed),
                    # ran the SAME down file a second time, waited on this row,
                    # deleted NOTHING, and committed its duplicate rollback
                    # reporting success: `times 002's rollback SQL executed: 2`,
                    # both runs "down 002_second.sql", exit 0.
                    #
                    # Rolling back on a zero count is what undoes it: the
                    # duplicate down SQL is in this same transaction and goes
                    # with it, so the destructive work is discarded rather than
                    # merely reported.
                    if removed.rowcount != 1:
                        raise MigrationError(
                            f"{migration.filename} was already removed from the history by "
                            "another run, so two runs overlapped and this one has just "
                            f"executed {migration.down_filename} a second time. That work has "
                            "been rolled back and nothing further is unwound. Check that only "
                            "one migration run is started per deploy, then re-run"
                        )
                if migration.filename in adopted:
                    # After the fact, because it says "executed": announcing it
                    # up front claimed execution for every unverifiable rollback
                    # in the set, including any the run never reached.
                    emit(
                        f"adopt {migration.filename}  "
                        "(unverified rollback executed on operator request)"
                    )
                expected.discard(migration.filename)
                emit(f"down  {migration.filename}")
        finally:
            # Nested, not sequential: _restore_session suppresses Exceptions but
            # a cancellation delivered inside it would otherwise skip the unlock
            # entirely and strand the lock — which is what the cancellation test
            # caught when these were two statements in a row.
            try:
                await _restore_session(conn)
            finally:
                await _unlock(lock_conn, emit=emit)
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
        # _ensure_bookkeeping runs `RESET search_path`, and the commit below makes
        # that persistent on a connection that then goes back to the pool: a
        # caller who had set a session search_path got the database default back
        # on their next query. Measured: `tenant, public` became `"$user",
        # public`. Reading the history is not a reason to change the session it
        # was read on.
        #
        # RESTORED rather than discarded, which is where this differs from the
        # writers. What they drop is state a MIGRATION left, and dropping it is
        # the point. Here the state is the caller's own, so ending the session
        # loses it just as thoroughly as leaking the reset does — measured, the
        # discard lands on the same `"$user", public`. Only putting it back is
        # actually a fix.
        entry_path = str(await conn.scalar(sa.text("SHOW search_path")))
        try:
            ledger = await _ensure_bookkeeping(conn)
            await conn.commit()
            applied = await _applied_rows(conn, ledger)
        finally:
            with suppress(Exception):
                await conn.execute(
                    sa.text("SELECT pg_catalog.set_config('search_path', :p, false)"),
                    {"p": entry_path},
                )
                await conn.commit()

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
            # The symmetric difference holds both halves of the disagreement:
            # the applied migration that jumped the queue AND the pending one it
            # jumped over. Calling the pending file "applied out of sequence"
            # contradicted the `applied` flag on the very same row.
            drift = (
                "applied out of sequence (history is not a prefix)"
                if row is not None
                else "pending behind an applied migration (history is not a prefix)"
            )
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
