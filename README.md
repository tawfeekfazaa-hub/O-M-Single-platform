# AQ O&M Platform

Unified O&M platform for Arabian Qudra Solar: live + historical monitoring,
alarms, IEC 61724-1 KPIs, and (later) predictive analytics and PM/CM
maintenance — multi-vendor, starting with Huawei FusionSolar.

## Layout

```
backend/    Python 3.11 + FastAPI + TimescaleDB (adapters, scheduler, API)
frontend/   Next.js dashboard (App Router)
docs/       PRD, vendor API notes, architecture decision log
```

Read `CLAUDE.md`, `docs/PRD.md` and `docs/API-NOTES.md` before touching code.

## Backend quickstart

Linux/macOS:

```bash
cd backend
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest                      # runs fully offline against the mock adapter
SCHEDULER_ENABLED=true uvicorn app.main:app --reload --reload-dir app
```

Windows (PowerShell):

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
pytest
$env:SCHEDULER_ENABLED = "true"
python -m uvicorn app.main:app --reload --reload-dir app
```

Always pass `--reload-dir app` with `--reload`: without it the watcher also
watches `.venv/` and restarts the server on every package change.

With no `.env`, the API runs in **mock mode** with an in-memory store and
(if `SCHEDULER_ENABLED=true`) ingests data from the FusionSolar mock
adapter — no credentials, no database needed.

## Database (staging/prod or local full-stack)

```bash
docker compose up -d timescaledb
cd backend && python scripts/apply_migrations.py       # uses DATABASE_URL
python scripts/apply_migrations.py --status            # what is applied
python scripts/apply_migrations.py --down-to base      # roll everything back
```

An applied migration is immutable: its checksum is verified on every run and an
edit refuses the whole run. Rollback, recovery and the rules for writing a
migration are in `docs/MIGRATIONS.md`.

### Live-database tests

The default `pytest` run is offline and needs no database. The migration and
repository tests need a real PostgreSQL/TimescaleDB and are deselected unless
asked for:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://aq_om@127.0.0.1:5432/postgres \
  pytest -m dbtest
```

Each test creates and drops its own database, so the one in the URL is only used
to connect. That is a connection point, **not** the extent of what the role
needs: these tests are not runnable by an ordinary application role.

- **`CREATEDB`** — every test creates and drops a database of its own, because
  migrations are DDL and a shared schema would make one test's rollback another
  test's missing table. Without it the fixtures fail before any test body runs.
- **Permission to install TimescaleDB** — `001` runs `CREATE EXTENSION
  timescaledb`, which needs a superuser or equivalent on most installations, and
  the extension available on the server.

So point `TEST_DATABASE_URL` at a **throwaway server** you have that much
control over — the `docker-compose.yml` one does, and is the intended target —
not at a shared or production cluster. The application's own runtime role needs
none of this; see "Role provisioning is not a migration" in `docs/MIGRATIONS.md`
for the split.

CI runs these in the `backend-db` job against the same pinned TimescaleDB image
as `docker-compose.yml`.

## Frontend quickstart

```bash
cd frontend
npm install
npm run dev    # http://localhost:3000/plants — expects backend on :8000
```

Run only ONE `npm run dev` at a time — a second instance falls back to
another port and fights over the shared `.next/` build folder (EPERM on
`.next/trace` on Windows).

## Configuration

All configuration via environment variables — see `.env.example`
(names only; real values live in your untracked `.env`).

## Security & development rules

- **Credentials never leave `.env`.** Real Huawei/FusionSolar credentials
  must never appear in GitHub (code, commits, branches), CI configuration
  or logs, AI prompts, tests or fixtures, screenshots, or issue/PR
  comments. `.env` is gitignored; `.env.example` carries names only.
- **No public exposure.** The local API (`:8000`), the Next.js dev server
  (`:3000`), and PostgreSQL (`127.0.0.1:5432` only) are development
  services: never bind them to a public interface or forward them to the
  internet.
- **Real-mode ingestion is prohibited for now — and enforced.** The app
  refuses to start `FUSIONSOLAR_MODE=real` + `SCHEDULER_ENABLED=true`
  until Raw/Quarantine storage lands (PR-2). Development and CI run
  exclusively against the mock adapter. Rollout: PR-1 validated the
  connector contract offline; PR-2 adds Raw/Quarantine; only after PR-2
  **plus** an approved staging host **plus** the company data-location
  policy decision may a controlled live check run.
- **`backend/scripts/check_fusionsolar.py` live mode stays PROHIBITED**
  until those same three conditions hold. Its default run is an offline
  dry-run; the hardened live path additionally demands `--live`,
  `--i-understand-rate-budget`, real mode, full configuration and a
  disabled scheduler — and prints counts only, never station identities,
  values, tokens, or credentials. A green mock run (or green tests) is no
  evidence that a real Huawei connection works.
- CI enforces: ruff + pytest (offline), the migration/repository suite against
  a real TimescaleDB, `pip-audit` on the fully resolved production
  tree and on the complete installed environment (blocking on any finding),
  `npm audit` blocking on high/critical for production and for all
  dependencies, ESLint, `tsc --noEmit`, production build, a `/plants`
  smoke test against the built app, and a full-git-history TruffleHog scan
  (checksum-pinned binary, no floating container tag).
- **Known limitation**: the Python dependency tree is NOT hash-locked yet —
  `requirements.txt` pins direct dependencies only, so builds are not fully
  reproducible. Required follow-up before any deployment: generate hashed
  lockfiles (e.g. `pip-compile --generate-hashes`) and install with
  `--require-hashes`.

## Hard rules

See `CLAUDE.md`. Highlights: no hardcoded credentials, all vendor calls go
through the central scheduler (FusionSolar allows ~5 calls/10 min), every
adapter has a mock mode, tests + CI must pass before merge.
