# Architecture Decision Log

Format: newest first. Every change to the fixed architecture in CLAUDE.md
requires an entry here.

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
