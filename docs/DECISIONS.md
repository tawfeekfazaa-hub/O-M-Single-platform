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
   an unverified baseline. The checksum is taken over newline-normalized content
   so a CRLF checkout (README documents a Windows workflow) cannot brick it, and
   that same normalized text is what gets executed, so "checksummed" and
   "executed" cannot diverge if a file is replaced mid-run.
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
