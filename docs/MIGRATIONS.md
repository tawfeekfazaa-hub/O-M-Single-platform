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

`--status` exits `2` when any migration has drifted from what was applied, so it
can be used as a deployment gate.

Exit codes: `0` success, `1` no `DATABASE_URL`, `2` the run was refused (the
message says why). A refusal never leaves the schema half-changed.

## The guarantees

1. **An applied migration is immutable — and so is its rollback.** A SHA-256 of
   the forward file *and* of its `.down.sql` is stored on first apply and
   re-verified on every later run. Editing either refuses the whole run,
   including any pending migrations behind it. The checksum is taken over
   newline-normalized content, so a CRLF checkout does not trip it.
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
   other process blocks.
5. **Safe to re-run.** Re-applying is a no-op, so a retried deploy is harmless.
6. **Every migration has a tested way back.** Each `NNN_name.sql` has a paired
   `NNN_name.down.sql`, and rollback refuses to start unless *every* down file it
   would need is present and matches what was recorded.
7. **Nothing is trusted silently.** Rows from the pre-checksum runner are
   refused until an operator adopts them deliberately, and every failure —
   including a syntax error inside a migration — is reported as a refusal naming
   the file and the error type, never the SQL.

## Writing a migration

- Name it `NNN_lower_snake.sql` — the number is the apply order. Anything that
  cannot be ordered is refused.
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

**"these rollback files changed after their migration was applied"**
A `.down.sql` was edited after its forward migration ran. Nothing was unwound.
Restore the recorded rollback file; if the rollback genuinely needs to change,
that is a new forward migration, not an edit to history.

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
`TEST_DATABASE_URL` is only used to connect — it is never modified. In CI these
run in the `backend-db` job against the same pinned TimescaleDB image as
`docker-compose.yml`. A bare `pytest` deselects them, so the default suite stays
offline and skip-free.
