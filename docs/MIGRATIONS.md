# Database migrations — running, rolling back, recovering

Numbered plain-SQL files in `backend/migrations/`, applied by
`backend/scripts/apply_migrations.py` (runner: `backend/app/db/migrations.py`).
See ADR-003 for why plain SQL, ADR-006 for the guarantees below.

## Commands

```bash
cd backend
export DATABASE_URL=postgresql+asyncpg://...

python scripts/apply_migrations.py                              # apply everything pending
python scripts/apply_migrations.py --status                     # what is applied, what is not
python scripts/apply_migrations.py --down-to 001_initial_schema.sql   # undo everything after it
python scripts/apply_migrations.py --down-to base               # undo everything
python scripts/apply_migrations.py --adopt-legacy-checksums     # see the recovery notes
```

`--status` exits `2` when any migration has drifted from what was applied — an
edited or missing file, an edited, removed or newly appeared rollback file, an
unverifiable legacy row, or a history that is no longer a prefix of the
sequence — so it can be used as a deployment gate.

Exit codes: `0` success, `1` no `DATABASE_URL`, `2` the run was refused (the
message says why). A refusal never leaves the schema half-changed. A database
that cannot be reached is a refusal too, reported by exception type rather than
message — a connection error's text carries the host and port.

## The guarantees

1. **An applied migration is immutable — and so is its rollback.** A SHA-256 of
   the forward file *and* of its `.down.sql` is stored on first apply and
   re-verified on every later run. Editing either refuses the whole run,
   including any pending migrations behind it. The checksum is over the file's
   exact bytes — nothing is normalized — so two files that would execute
   differently can never share one. Line endings are pinned instead by
   `.gitattributes` (`*.sql text eol=lf`), and a test asserts no migration in
   this repository carries a CR.
2. **What is executed is what was checksummed.** Each file is read once, at
   discovery; that exact text is both hashed and executed. A file replaced
   mid-run cannot record one hash and run different SQL.
3. **History is a prefix.** Applied migrations must be the first N of the
   sequence. A migration numbered behind ones already applied is refused —
   applying it would make the recorded order a lie and send rollback, which
   unwinds in reverse filename order, down the wrong path.
4. **One writer at a time.** A session-level advisory lock is taken before
   anything else and released explicitly, so it is not left held on a pooled
   connection where the next call from the same process succeeds while every
   other process blocks. It is held on a connection of its own, which the
   migrations cannot reach — so the runner needs an engine able to supply **two**
   connections, and refuses up front on one that cannot. That connection sits
   idle while a migration runs, so the lock is re-checked immediately before
   every history row: if its session was closed underneath us (an idle timeout,
   a proxy), the run stops there rather than committing under a lock somebody
   else may now hold.
5. **Safe to re-run.** Re-applying is a no-op, so a retried deploy is harmless.
6. **Every migration has a tested way back.** Each `NNN_name.sql` has a paired
   `NNN_name.down.sql`, and rollback refuses to start unless *every* down file it
   would need is present and matches what was recorded.
7. **A migration and its history row commit together.** The migration SQL and
   the `schema_migrations` write share one transaction, so the schema can never
   advance without its history — or the reverse. A file that manages its own
   transaction would break that pair, so it is refused before it runs, and the
   transaction is checked again after it has run.
8. **Nothing is trusted silently.** Rows from the pre-checksum runner, and
   rollback files that appeared after their migration was applied, are refused
   until an operator adopts them deliberately. Every failure — including a
   syntax error inside a migration, or a database that disappears mid-run — is
   reported as a refusal naming the file and the error type, never the SQL.
9. **Every refusal has a documented way forward** that does not involve editing
   `schema_migrations` by hand.
10. **There is one ledger, and the runner will not quietly make a second.** A
   second, empty `schema_migrations` reads as "nothing has ever been applied",
   so the next run re-applies everything. Before creating one, the runner looks
   for an existing `schema_migrations` across the whole database; if one exists
   but is not on this connection's `search_path`, the run is refused and names
   where it is. See "schema_migrations is not on this connection's search_path"
   below.
11. **The connection a migration ran on is dropped, not pooled.** A migration
   can leave `search_path`, `statement_timeout`, `SET ROLE`, `LISTEN`
   registrations and temporary objects behind, and a caller sharing the engine
   would inherit all of it. Ending the session is the only reset that covers
   every kind at once. Cost: one reconnect per run.

## Writing a migration

- Name it `NNN_lower_snake.sql` — the number is the apply order. Anything that
  cannot be ordered is refused.
- **Do not wrap it in `BEGIN`/`COMMIT`.** The runner already runs each migration
  in one transaction together with its history row; a file that commits itself
  breaks that pair and is refused. This is the one difference from a script you
  would run by hand in `psql`, and it is the usual reason a working script is
  rejected on the way in. (A `COMMIT` inside a `$$ … $$` function body, a string
  or a comment is fine — only real transaction control counts, and a
  `BEGIN ATOMIC … END` function body is recognised as a body, not a
  transaction — though only for the `END` that closes it.)
- Migrations are executed with `standard_conforming_strings = on`, set on the
  migration's own transaction, whatever the database or role default is. So a
  backslash in an ordinary `'...'` literal is a backslash; use `E'...'` if you
  want escapes. This is not a style preference: with the setting off, the same
  file means two different things to the server and to the guard above, and a
  `COMMIT` the guard reads as quoted text is one the server executes.
- Anything that cannot run inside a transaction — `CREATE INDEX CONCURRENTLY`,
  `VACUUM` — cannot be a migration here. Do it as an operator step.
- `SET search_path` inside a migration is allowed and affects the statements
  after it, including later migrations in the same run. It cannot affect the
  runner: the ledger's schema is resolved before any migration runs, every
  bookkeeping statement runs with the path pinned to `pg_catalog` (which pins
  the `=` operator too, not just the table), each run starts by resetting the
  path, and the connection is dropped rather than returned to the pool.
- **Do not change the database's or the role's default `search_path` from a
  migration.** `ALTER DATABASE … SET search_path` / `ALTER ROLE … SET
  search_path` outlive the session, the pool and the process, and the next run
  resets to the value you left. If that value no longer reaches
  `schema_migrations`, the run is refused (guarantee 10) — and before that
  refusal existed, it silently created a second ledger and re-applied every
  migration. Setting a default search_path is an operator step, like role
  provisioning.
- The run lock is held on a **separate connection**. A migration cannot release
  it — `pg_advisory_unlock_all()` in a migration affects only its own session.
- If a value needs a carriage return in it, write the escape — `E'a\r\nb'` —
  rather than a physical CRLF in the file. Line endings are normalized by git on
  the way in and out, so they are not a reliable way to carry data, and a CR in
  a migration file will fail the check above.
- Write the paired `NNN_lower_snake.down.sql` at the same time. A migration
  without one can be applied but blocks any rollback past it.
- Prefer additive changes (new tables, nullable columns). They make the down
  file a pure `DROP` and keep rollback non-destructive to existing rows.
- **Never edit a migration that has been applied anywhere.** Add a new one.
- Migrations contain no credentials, no role passwords and no environment
  values. Role and grant *provisioning* is an operator step (below), not a
  migration.

## When something goes wrong

**"these migrations were edited after being applied"**
Someone changed a file that a database has already run. Do not force it. Either
restore the file to its applied content (`git log -p` on the file), or — if the
change is genuinely wanted — add a new migration that makes the change. The
checksum is doing its job: it is telling you the recorded history no longer
describes the database.

**"these migrations were applied by a runner that recorded no checksum"**
The database was migrated by the pre-checksum runner, so what actually ran there
is unknown. Do not adopt reflexively: confirm the schema matches the current
files (compare against a freshly migrated database), then re-run with
`--adopt-legacy-checksums`. The adoption is recorded as an **unverified**
baseline and says so in its output.

**"these rollback files were edited or removed after their migration was applied"**
A `.down.sql` changed or disappeared after its forward migration ran. This is
checked on **every** run, not only when rolling back — letting the schema
advance while its recovery path is known to be corrupt is exactly the moment a
rollback is most likely to be needed. Restore the recorded rollback file; if it
genuinely needs to change, that is a new forward migration, not an edit to
history.

**"these migrations were applied with no rollback file, and one has since appeared"**
The recorded `down_checksum` is NULL, which is evidence that this file did not
accompany the applied migration — it could contain anything. Review it, then
choose:

- to keep the migration applied and simply record the file, run
  `apply_migrations.py --adopt-legacy-checksums`. It stores the checksum in
  place, clears the `--status` drift, and authorises that exact text to run as
  the rollback later.
- to roll back right now, pass the same flag to `--down-to`. The execution is
  announced as unverified.

Either way the adoption rests on nothing but your review of the file, which is
why neither happens without the flag.

**"NNN_name.sql manages its own transaction (BEGIN, COMMIT)"**
The file is written as a standalone script. The runner supplies the transaction
— together with the `schema_migrations` row, so the two commit or fail as one —
and a file that commits itself silently breaks that. Delete the
transaction-control statements; nothing else needs to change. Nothing was
applied, including the migrations ahead of it in the same run.

**"NNN_name.sql ended the runner's transaction before its schema_migrations row
could be written"**
The safety net behind the check above, for a way of ending the transaction the
file scan did not recognise (a procedure that commits internally, for example).
Unlike every other refusal here, this one reports damage rather than preventing
it: what the migration did is already committed, with no history row, so the
next run would apply it a second time. Remove the transaction control, then
reconcile by hand — either undo what it did, or insert its `schema_migrations`
row once you have confirmed the schema matches the file.

**"two migrations are numbered NNN: A and B"**
A reused sequence number, usually from a branch merge. Renumber one to the end
of the sequence. Filename order would otherwise hide it from the prefix check
below and apply both, leaving the numeric revision ambiguous.

**"applied migrations are not a prefix of the migration sequence"**
A migration is numbered behind ones already applied — typically a branch merge
that reused a number. Renumber it to the end of the sequence. Applying it in
place would record an order that never happened.

**"NNN_name.sql failed to execute: ..."**
The SQL itself failed. Its transaction rolled back and nothing was recorded for
it, so the fix is to correct the file (or add a new one, if it was already
applied elsewhere) and re-run. The message names the file and the error type
only — the SQL and any values stay out of logs.

**"these migrations are recorded as applied but are no longer present"**
A migration file was deleted or the checkout is on a branch that predates it.
Check out the commit that has the file. Repairing `schema_migrations` by hand is
a last resort and should be recorded in the incident notes.

**"another migration run holds the advisory lock"**
A concurrent deploy is running, or a previous run died with its connection still
open. Wait; if nothing is running, the lock is released as soon as the orphaned
session ends. `SELECT * FROM pg_locks WHERE locktype = 'advisory'` shows the
holder.

**"note: the advisory lock could not be released cleanly"**
Not a failure of the run — it accompanies whatever the real result was. The
connection was discarded instead, which ends its session and releases the
session-scoped lock with it, so the next run is not blocked. It appears when the
database became unreachable during the run; the refusal printed alongside it is
the one to act on.

**"the run lock is no longer held"** / **"lost contact with the session holding
the run lock"**
The connection carrying the advisory lock was closed while a migration ran — an
`idle_session_timeout`, a connection proxy, or an administrator terminating the
backend. A session-scoped lock dies with its session, so another run could have
started. The migration in flight is rolled back and nothing further is applied;
earlier migrations in the same run stay applied and recorded, as they always do.
Check that no second run is in progress, then run again. If it recurs, raise
`idle_session_timeout` for the migration role, or run migrations somewhere
without a proxy in the path.

**"could not open a second connection ... fewer than two"**
The run lock needs a connection of its own. Give the runner an engine whose pool
can supply two (the CLI's own engine always can); an engine created with
`pool_size=1, max_overflow=0` cannot.

**"schema_migrations is not on this connection's search_path, but one already
exists at X"**
The ledger exists at `X`, but this connection's `search_path` does not reach it,
so applying anything would mean creating a second, empty ledger — which reads as
"nothing has ever been applied" and re-applies every migration. Nothing was
applied. Usually the database's or the role's default `search_path` changed
(`ALTER DATABASE … SET search_path`, `ALTER ROLE … SET search_path`), possibly
from inside a migration, which is why migrations must not set it (see "Writing a
migration"). Fix by putting `X`'s schema back on the default path, or move the
ledger deliberately:

```sql
ALTER TABLE X SET SCHEMA the_schema_you_want;   -- with no run in progress
```

Do not create the second table to get past it.

**"cannot roll back to NNN_name.sql: it is not applied"**
The target exists in the checkout but the database never reached it. Nothing was
unwound, and the message says which migration the database is actually at.
Rolling back to a target you are already behind is not a no-op — reporting it as
success would tell automation it is at a revision it has never been at.

**"cannot roll back — these migrations have no .down.sql"**
Nothing was unwound. Write the missing down file first, then re-run. This check
runs before any rollback begins precisely so a missing file cannot leave the
schema in a state no file describes.

## Rollback is destructive

`--down-to` runs the down files, and a down file for a table-creating migration
drops the table. `001_initial_schema.down.sql` discards every plant, KPI
measurement and alarm. Take a backup before rolling back anything that holds
data you cannot re-ingest, and remember that the vendor's rate budget makes
re-ingestion slow (the station-list allowance is a handful of calls per day).

The `timescaledb` extension is deliberately not dropped by any down file: other
databases in the cluster may depend on it.

## Role provisioning is not a migration

The migration runner needs DDL rights on the target database, but it is **not
assumed to be a superuser and not assumed to be able to create roles**. Creating
the application and maintenance roles, and granting them, is an operator step
performed once per environment, outside the migration files — so that no
migration ever contains a role name tied to a password, and so that a
least-privilege application role cannot grant itself more.

The concrete roles and grants arrive with the Raw/Quarantine schema (PR-2A1),
which is also where the API/ingestion privilege split is defined. Until then the
only requirement is: the account in `DATABASE_URL` must be able to create tables
in the target database, and `CREATE EXTENSION timescaledb` must already be
possible (the extension is available in the pinned TimescaleDB image).

## Testing migrations

`backend/tests/test_migration_runner.py` exercises the runner's contract against
synthetic migrations; `backend/tests/test_db_schema.py` applies the real ones,
rolls them back and re-applies them. Both are marked `dbtest` and need a live
database:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://user@127.0.0.1:5432/postgres \
  pytest -m dbtest
```

Each test creates and drops its own database, so the one named in
`TEST_DATABASE_URL` is only used to connect — it is never modified, and it is
the database actually used for that connection, so the role needs no access to
the cluster's `postgres` database. In CI these
run in the `backend-db` job against the same pinned TimescaleDB image as
`docker-compose.yml`. A bare `pytest` deselects them, so the default suite stays
offline and skip-free.
