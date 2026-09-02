# Architecture Decision Log

Format: newest first. Every change to the fixed architecture in CLAUDE.md
requires an entry here.

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
   so a truncated list can never pass as the complete fleet.
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
