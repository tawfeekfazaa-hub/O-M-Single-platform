# Architecture Decision Log

Format: newest first. Every change to the fixed architecture in CLAUDE.md
requires an entry here.

## ADR-006 — 2026-09-03 — Migration harness and the TimescaleDB reference rule (PR-2A0)

Groundwork for the Raw/Quarantine storage of PR-2A1. No schema change: this
entry records how migrations are governed and one measured database constraint
that decides the next schema's shape.

1. **Migrations stay numbered plain SQL** (ADR-003 upheld). PR-2 adds several
   tables, a partial index and triggers — all DDL-first and awkward through an
   ORM migration framework — and introducing such a framework in the same change
   as the schema would double the review surface. Revisit as a standalone change
   after PR-2.
2. **An applied migration is immutable, and so is its rollback.**
   `schema_migrations` records a checksum for the forward file AND for its
   `.down.sql`; every run re-verifies both and refuses the whole run on a
   mismatch or a deleted file. A rollback file is as destructive as the forward
   file is constructive — one edited after the fact was observed dropping a
   table belonging to a different migration while every preflight passed — so it
   gets the same protection. Rows written by the PR-1 runner have no checksum
   and are **refused** rather than adopted: recording the current file's hash
   would declare the database verified against SQL that may never have run
   there, which is the exact drift the checksum exists to reveal. Adoption is a
   deliberate operator action (`--adopt-legacy-checksums`) and is announced as
   an unverified baseline — after the transaction commits, never before, so the
   announcement cannot outlive a write that a later refusal rolls back. The
   checksum is over the file's **exact bytes**, which are also what executes, so
   "checksummed" and "executed" cannot diverge if a file is replaced mid-run.
   Newline normalization was tried here and withdrawn; see 4h for why, and for
   where the CRLF-checkout problem went instead.
2b. **History must be a prefix.** Applied migrations have to be the first N of
   the discovered sequence. With 001 and 003 applied, a 002 appearing later
   would be applied *after* 003 while the bookkeeping implied filename order —
   and rollback, which unwinds in reverse filename order, would then run down
   files in an order that never happened.
2c. **`--status` reports drift instead of hiding it.** It is the command an
   operator reaches for when something looks wrong, so it must show the same
   inconsistencies that apply and rollback refuse to run with — including an
   applied migration whose file is gone, which used to vanish from the output
   entirely.
3. **One writer at a time.** A session-level advisory lock, taken before any
   bookkeeping; a concurrent run fails immediately rather than interleaving DDL.
4. **Every migration ships a paired `.down.sql`**, and `--down-to` verifies the
   whole set of down files before unwinding anything — a missing rollback file
   must not be discovered half-way. Rollback is destructive by nature and
   documented as such in docs/MIGRATIONS.md.
4b. **A migration and its history row are one transaction.** SQLAlchemy's
   transaction is lazy — `begin()` creates the object, but asyncpg opens the
   real transaction only when a statement passes through the adapter. Because
   migration SQL must go to the raw driver (asyncpg forbids multi-statement
   prepared statements), the transaction is forced open first. Measured before
   the fix: DDL run through the driver survived a rollback that should have
   discarded it, which would leave a schema advanced with no history.
4c. **A migration may not manage its own transaction.** That pairing is only
   worth as much as the transaction it depends on, and a file wrapped in
   `BEGIN`/`COMMIT` — the shape an author gets by pasting in a script that ran
   standalone — commits itself before the bookkeeping row is written. Measured:
   with a `COMMIT` in the file, a failing bookkeeping INSERT left the table
   created and `schema_migrations` empty, exactly the state 4b closes. So files
   are scanned at discovery and refused, with comments, string literals and
   `$$ … $$` bodies excluded so a `COMMIT` that is merely *mentioned* is not a
   false alarm. Text analysis is the guard, not the guarantee: after every
   migration the runner asks the driver whether the transaction is still open,
   which holds however the transaction was ended.

   The guard is a **tokenizer**, not a search, and that distinction was
   learned the expensive way. Its first form blanked comments and quotes and
   then looked for keywords, with single-character look-back to decide what a
   `$` or an `E` meant. Review broke it four times over two rounds, and every
   break had the same shape: a delimiter, a string prefix or a keyword read out
   of the MIDDLE of an identifier — `foo$$`, `foo$E'...'`, `foo$BEGIN ATOMIC`,
   an identifier with a non-ASCII character before `$$`. PostgreSQL lexes an
   identifier greedily and `$` is an identifier character after the first, so
   consuming whole identifiers makes that entire class of misreading
   impossible instead of excluding its members one at a time. Patching the
   fourth corner would have invited a fifth.

   The rule the tokenizer follows is "read it the way the server does", and two
   later findings came from places it still did not: a `BEGIN ATOMIC` body
   belongs only to `CREATE [OR REPLACE] FUNCTION|PROCEDURE`, so nothing else
   may open one (`CREATE TABLE begin (atomic int)` is a table with a column,
   `SELECT * FROM begin atomic` a table with an alias); and PostgreSQL ends a
   `--` comment at a bare carriage return as well as a newline, which a CRLF
   normalizer leaves behind.

   Recorded from the same rounds, as measured false negatives rather than
   only fixed: `$` is legal inside an unquoted identifier, so
   reading the `$$` of `foo$$` as a dollar quote blanked the file through to
   the next `$$` and hid a real `COMMIT`; one `BEGIN ATOMIC` body used to
   exempt *every* `END` in the file rather than its own; and with
   `standard_conforming_strings` off the server read `'it\'s'` as one string
   where the guard read two, so a `COMMIT` the guard thought was quoted was
   real SQL — both tables committed with no history row. The first two are
   scanner fixes. The third is not fixable in the scanner, because the same
   text has two valid readings: the migration transaction now begins with
   `SET LOCAL standard_conforming_strings = on`, which makes the server's
   reading and the guard's the same one.
4d. **Cleanup never replaces the failure it is cleaning up after.** Rolling back
   and releasing the lock both run in `finally`, and both raise when the failure
   was the database going away — replacing the `MigrationError` the CLI knows
   how to report with a closed-connection traceback. Measured: a migration whose
   backend was terminated surfaced as `InterfaceError: cannot call
   Transaction.rollback()`. Cleanup failures are now suppressed in favour of the
   original error, and the connection is discarded rather than returned to the
   pool, which ends its session and the session-scoped lock with it. That
   handler catches `BaseException`, not `Exception`: `asyncio.CancelledError`
   inherits straight from `BaseException`, and a caller cancelling mid-cleanup
   otherwise skipped it entirely and returned a still-locked session to the
   pool — where the same pool's next call succeeds reentrantly while every
   other process blocks, so the leak is invisible from inside the process that
   caused it. Measured with cancellation delivered before `pg_advisory_unlock`:
   another engine could not take the lock.
4l. **The runner does not share a session with the migrations it runs, where it
   matters.** Migration SQL executes on the runner's connection, so everything
   the runner resolves by name or keeps in session state is reachable from it.
   Three measured consequences, all closed: a migration containing
   `SELECT pg_advisory_unlock_all()` released the run lock without ending its
   transaction, letting a second runner in mid-migration — the lock now lives on
   its own connection, which migration text cannot reach. A down migration
   defining `shadow.=(text, text)` ahead of `pg_catalog` made the ledger DELETE
   match nothing, leaving a rolled-back migration recorded as applied (the
   mirror, an `=` returning true, would have deleted the whole history) — every
   bookkeeping statement now runs under `SET LOCAL search_path = pg_catalog`,
   and the advisory-lock functions are `pg_catalog`-qualified. And a migration's
   `SET search_path` rode back into the pool, so a library caller's next query
   resolved unqualified names in the migration's schema — the connection a
   migration ran on is now dropped rather than returned to it. `RESET
   search_path` closed only the case that was found: `statement_timeout`,
   `SET ROLE`, `LISTEN` registrations and temporary objects ride back the same
   way. `DISCARD ALL` covers all of them and was tried, but it runs `DEALLOCATE
   ALL`, leaving asyncpg's per-connection statement cache naming prepared
   statements the server has forgotten — measured, 17 tests in the runner's own
   module failed with `prepared statement "__asyncpg_stmt_1d__" does not exist`
   on the next caller to reuse that pooled connection. Ending the session
   discards every kind of state at once, including kinds nobody has thought of
   yet, for the price of one reconnect on a connection used once per run.
4m. **The lock must still be held when the history row is written.** The run
   lock lives on a connection of its own (4l), and that connection is idle for
   as long as a migration takes — `idle_session_timeout` or a proxy can close
   it, and a session-scoped advisory lock dies with its session. Measured: with
   the lock session terminated mid-migration, a second runner took the key while
   the first carried on and committed its history under a lock it no longer
   held. The lock session is re-checked immediately before each history row,
   which is the last moment the work can still be abandoned; the check doubles
   as a keep-alive, since between migrations that session is no longer idle.
4n. **Migrations need an engine that can supply two connections.** A consequence
   of 4l worth stating rather than leaving to be discovered: with `pool_size=1,
   max_overflow=0` the second checkout blocked until `pool_timeout` and surfaced
   as a pool error saying nothing about why. The runner now refuses up front and
   says what it needs.
4o. **A temporary table can never be the ledger.** PostgreSQL searches the
   session's implicit `pg_temp` schema before `search_path` for relation names —
   after `RESET search_path` too — and a temporary table has `relkind = 'r'` like
   any other. So a temporary `schema_migrations` on the work connection was what
   `to_regclass` answered with, and discovery accepted it. Measured on an engine
   whose pooled connections carried one: an empty history, every migration
   re-applied, a data migration's row inserted twice, exit 0 — and then the
   session ends (4-11) and the "history" goes with it, leaving the schema
   changes committed and unrecorded. Discovery now ignores temporary relations
   and refuses when one is shadowing the ledger, rather than continuing past a
   ledger-shaped table nobody put there on purpose.
4p. **The ledger mutation must notice that another run got there first.** 4m
   closes most of the window between confirming the lock and committing, not all
   of it: the lock session can be closed inside it and a second runner take the
   key while the first transaction is still uncommitted. Forward, `filename` is
   the ledger's primary key, so the loser's `INSERT` conflicts — but the
   `DELETE` in a rollback has no constraint to trip and matched nothing
   silently. Measured: with the lock session closed just after the check passed,
   the second runner read the row as still applied, executed the SAME down file
   a second time, deleted nothing, and committed its duplicate reporting success
   — `times the rollback SQL executed: 2`. Both mutations are now checked, and
   because the duplicate work shares the transaction being refused, rolling back
   discards it rather than merely reporting it.
4r. **The run lock is never taken on a session that already holds it.** Session
   advisory locks are reentrant: taking one this session already holds succeeds
   and raises the hold count, while the single release lowers it by one — so the
   lock stays held on a connection returned to the pool, blocking every other
   process, while this pool's next run takes it again reentrantly and never
   notices. Measured: two acquires and one release leave it held. The runner
   refuses on a session already holding the key, and — the structural half —
   ends the lock session after releasing, exactly as the failure path already
   did, so the runner can never be what leaked one.
4s. **An empty migrations directory is a refusal, not a no-op.** A deployment
   artifact that lost its migrations would create an empty ledger, print
   `applied 0 migration(s)` and exit 0 with no schema installed. Every other
   "nothing to do" in this runner is backed by a history saying so; this one was
   backed by nothing.
4q. **The lock session is left idle, not idle in transaction.** 4m's check runs
   a query on the lock connection, which opens a transaction there; left open,
   the session sat `idle in transaction` for the rest of the run. That is the
   state operators kill hardest — `idle_in_transaction_session_timeout` is
   commonly set where `idle_session_timeout` is not — so the check added to
   survive an idle timeout had made its own connection a better target for one.
   It ends its transaction now; a session-level advisory lock is unaffected.
4j. **The ledger cannot be redirected by the migrations it records.** Migration
   SQL runs on the runner's connection and may legitimately `SET search_path`,
   which would resolve a later unqualified `schema_migrations` elsewhere.
   Measured: a migration that created `app.schema_migrations` and set the path
   wrote its history row there, leaving the real ledger empty — so the next run
   saw the migration as unapplied. The ledger's schema is resolved once, before
   any migration runs, and every later statement is qualified with it; the
   search path is reset at the start of each run, because a `SET` from a
   previous run rides back on the pooled connection and would otherwise poison
   that resolution. `RESET` restores the *configured* default, though, and that
   is also within a migration's reach: `ALTER DATABASE … SET search_path = evil`
   outlives the session, the pool and the process. Measured on a fresh engine
   afterwards — RESET yielded `evil`, `CREATE TABLE IF NOT EXISTS
   schema_migrations` created a SECOND, empty ledger there, and the run silently
   re-applied an already-applied migration, putting a data migration's row in
   twice. (`CREATE TABLE IF NOT EXISTS` checks only the schema it would create
   in, not visibility, so it duplicated the ledger even with the real one on the
   path and plainly visible.) The runner now looks for `schema_migrations`
   across the whole database before creating one: not on the path but present
   elsewhere is refused, naming where it is, because a second ledger reads as
   "nothing has ever been applied". Refusing only in that exact transition
   leaves a database that legitimately hosts several applications, each with its
   own ledger in its own schema, working — each path finds its own. What stays
   outside the runner's reach: a migration that creates a complete, pre-filled
   ledger earlier on a redirected path is indistinguishable from an operator
   relocating one. That is review's job, not the runner's. An operator does not
   need `ALTER DATABASE` to arrive here either — retargeting `ALTER ROLE … SET
   search_path` gets there by accident.
4k. **Two migrations may not share a sequence number.** Filename order hides a
   reused number from the prefix rule — `002_beta` sorts after `002_alpha`, so
   the applied set stays a prefix and a second `002` is applied. "We are at 002"
   would then name two different schemas.
4i. **Reporting never changes the outcome it reports.** Every announcement is
   made after the work it describes commits, so an exception from the caller's
   `emit` — a closed stdout, a logging handler that raises — turned a completed
   run into `migration refused` and exit 2. Measured: a `BrokenPipeError` from
   `emit` left the table created and its history row written, and the CLI
   reported a refusal. Reporting failures are now suppressed; the runner's own
   are not.
4f. **A database problem is a refusal, not a traceback.** The documented exit
   contract is 0/1/2, and the runner's taxonomy only covers what the runner
   itself recognises. Measured: with the server not running the CLI printed a
   `ConnectionRefusedError` traceback, and a pooled connection killed by a
   restart escaped as an asyncpg `InternalClientError` — from the lock step,
   before any migration ran. The CLI now reports any unrecognised failure as a
   refusal naming the exception TYPE only, since a connection error's message
   carries the host and port.
4e. **Every refusal has a way forward that is not hand-editing
   `schema_migrations`.** A rollback file written *after* its migration was
   applied has a NULL recorded `down_checksum`, which rollback refuses and
   `--status` reports — but there was no way to vouch for it while keeping the
   migration applied, so the deployment gate stayed at exit 2 permanently and
   the only remedy on offer was to roll the migration back, destroying data to
   close a bookkeeping gap. `--adopt-legacy-checksums` now records it in place,
   under the same explicit-operator-action posture as 2.
4h. **The checksum is over the file's exact bytes.** It was briefly taken over
   newline-normalized content, so that a CRLF checkout could not change every
   hash and refuse every run. That folding was withdrawn: a physical CRLF inside
   a string literal is part of the value, so two files that insert different
   data hashed the same, and editing an applied literal from LF to CRLF passed
   the immutability check. Immutability that cannot see a difference the
   database will see is not immutability. The checkout problem moved to
   `.gitattributes` (`*.sql text eol=lf`), with a test asserting the outcome —
   no CR in any migration — rather than trusting the configuration.
4g. **The lock is taken inside the region that releases it.** PostgreSQL can
   grant the advisory lock and the caller be cancelled before the runner sees
   the result, so acquiring it before entering the `try/finally` left a window
   where a locked session went back to the pool with nothing arranged to
   release it. `pg_advisory_unlock` only ever releases a lock held by the
   calling session, so running it when the lock may not have been granted
   cannot disturb the run that does hold it.
5. **Role provisioning is not a migration.** The runner is not assumed to be a
   superuser or able to create roles; roles and grants are an operator step, so
   no migration ever carries a role name bound to a password. The concrete
   API/ingestion privilege split lands with PR-2A1.
6. **Live-database tests are a first-class CI job.** `backend-db` runs against
   the same pinned TimescaleDB image as docker-compose, applies the real
   migrations, rolls them back, re-applies them, and exercises
   `PostgresRepository` — which had no test coverage at all before this change.
   The offline job deselects them (`-m "not dbtest"`) rather than letting them
   skip, and the DB job fails up front if the database is unreachable, so a
   green run can never mean "verified nothing".

**Measured constraint, deciding D2 for PR-2A1.** A TimescaleDB hypertable
refuses any unique index that omits its partitioning column, so a surrogate `id`
alone can never be unique on a hypertable — and a foreign key needs a unique
constraint on what it references. Verified against the pinned image in
`tests/test_db_schema.py`, not taken from documentation.

The claim is deliberately scoped to that. A foreign key to `id` alone is
rejected by *plain* PostgreSQL too, with the same "no unique constraint matching
given keys" message, so a test written that way would prove nothing about
TimescaleDB; a guard test pins that distinction so it is not lost later.

The only remaining candidate is the full composite key
`(id, partitioning_column)`, and TimescaleDB **does** accept a foreign key
referencing it — measured, after a first attempt asserted the opposite and CI
refuted it. So the choice for PR-2A1 is not "hard references are impossible" but
a trade-off it must make deliberately:

- a hard reference obliges every referencing row to carry the partitioning
  column as well (`kpi_measurements` would need a `raw_received_at` beside every
  `raw_payload_id`), and
- it couples raw retention to referential integrity: dropping a chunk of raw
  payloads that normalized rows still reference cannot leave those references
  dangling, which is precisely what a differing retention period requires.

The recommendation stands — soft references, and provenance pointing at a purged
payload reported as such — but it now rests on the retention trade-off rather
than on an impossibility. Whether `drop_chunks` is actually blocked by such a
reference is the concrete question PR-2A1 should settle before choosing.

Consequence: raw payloads stored in a hypertable (so retention is a chunk drop
rather than a mass DELETE) can only be referenced SOFTLY — a plain `BIGINT` with
no foreign key — from `kpi_measurements` and the quarantine tables. Provenance
pointing at a purged payload is therefore an expected state to be reported, not
corruption to be prevented. The opposite direction (a hypertable referencing a
regular table) is permitted and is what `kpi_measurements.plant_id` already
relies on; it is pinned by a test so PR-2A1 can build on it.

## ADR-005 — 2026-09-01 — FusionSolar contract hardening (PR-1)

Decisions (full contract: docs/FUSIONSOLAR-CONTRACT.md):

1. **legacy_system_code is the only API profile** (`FUSIONSOLAR_API_PROFILE`),
   speaking `/thirdData/getStationList`; the OAuth `/thirdData/stations`
   stack is a documented future upgrade with zero code and **no
   auto-fallback in either direction** — endpoint probing on failure would
   burn rate budget and mask auth/version errors.
2. **Per-endpoint rate budgets** replace the old single shared budget:
   login 4/600 s (official 5/10 min minus margin), real-time KPI
   ceil(plants/100)/5 min derived at runtime (official), station list
   4/day as a configurable SAFETY DEFAULT (the official daily formula
   varies by SmartPVMS version and is deliberately not encoded as a
   universal constant). Budgets are independent; no retry may spend
   another endpoint's budget.
3. **Inventory and KPI schedules are separate**: station inventory
   refreshes on a conservative 6-hour default cadence; KPI cycles read
   the repository cache and never call the station-list endpoint. Since a
   paginated inventory spends one station-list call per page, the
   scheduler stretches the effective refresh spacing to pages × window /
   budget, and a budget-rejected refresh defers itself without aborting
   KPI polling.
4. **Station-list pagination** (pageNo/pageSize=100) with both documented
   response variants on the same path, finite page guards, repeated-page
   detection, deterministic dedup, and no silent skipping of malformed
   pages. The paginated envelope is held to a STRICT contract —
   pageNo/pageSize/pageCount/total mandatory, stable across pages, final
   unique-station count reconciled against `total` — and a failed
   validation aborts the refresh without touching the stored inventory,
   so a truncated list can never pass as the complete fleet. Because a
   refresh cannot be resumed across rate-limit windows, the effective page
   guard is bounded by the station-list budget, and the refresh spacing is
   `window / floor(budget / pages)` — the number of COMPLETE bursts the
   rolling window can hold, never an average rate. A budget-rejected
   refresh defers a full window so its own partial burst has expired, and
   a cycle whose inventory is stale for ANY reason (rate limit or failure)
   is never reported as a complete ingestion.
5. **Real-mode safety gate**: the app refuses FUSIONSOLAR_MODE=real +
   SCHEDULER_ENABLED=true until Raw/Quarantine storage (PR-2) exists.
   The diagnostic checker is offline/dry-run by default and its live path
   stays prohibited until PR-2 + approved hosting + the data-location
   policy decision.
6. Real mapping never invents station active power (no documented field →
   None); the mock's synthetic fields are mock-only and documented as
   such. Vendor `params.currentTime` is carried on the reading as server
   time, never as a measurement timestamp; it is persisted (with the raw
   envelope) only from PR-2 Raw/Quarantine onward.

## ADR-004 — 2026-09-01 — Repository layer with in-memory + Postgres backends

The API and scheduler depend on a `Repository` protocol, not on SQLAlchemy
directly. Two implementations: `InMemoryRepository` (default when
`DATABASE_URL` is unset — dev, CI, unit tests) and `PostgresRepository`
(TimescaleDB, staging/prod). This keeps rule "tests run with zero
infrastructure" true while the SQL schema stays the source of truth for
persistence.

## ADR-003 — 2026-09-01 — Plain SQL migrations, no Alembic (Phase 1)

TimescaleDB features (hypertables, compression) are DDL-first and awkward
through ORM migrations. Phase 1 ships numbered SQL files in
`backend/migrations/` applied by `scripts/apply_migrations.py`. Revisit
Alembic when the schema churns.

## ADR-002 — 2026-09-01 — Custom asyncio scheduler, no APScheduler/Celery

The ingestion loop is one periodic task with backoff. A dependency-free
asyncio loop is easier to test (injectable clock/sleep) and removes a
broker/beat process. Revisit if we grow >3 independent schedules.

## ADR-001 — 2026-09-01 — Fixed platform architecture

- backend: Python 3.11 + FastAPI; frontend: Next.js (App Router).
- DB: PostgreSQL + TimescaleDB; time-series in hypertables.
- Vendor integrations only via adapters implementing
  `backend/app/adapters/base.py`; one central scheduler owns all real
  vendor API calls (FusionSolar ~5 calls/10 min, single session).
- Dashboards read exclusively from our DB/API, never vendor APIs.
