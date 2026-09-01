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
cd backend && python scripts/apply_migrations.py   # uses DATABASE_URL
```

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

## Hard rules

See `CLAUDE.md`. Highlights: no hardcoded credentials, all vendor calls go
through the central scheduler (FusionSolar allows ~5 calls/10 min), every
adapter has a mock mode, tests + CI must pass before merge.
