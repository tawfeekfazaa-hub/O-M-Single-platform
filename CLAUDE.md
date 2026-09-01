# AQ O&M Platform — Unified Solar Monitoring & Maintenance

## Mission
Unified O&M platform for Arabian Qudra Solar: live + historical monitoring,
alarms, KPIs (IEC 61724-1), predictive analytics, PM/CM maintenance module,
per-plant isolation, multi-vendor (Huawei FusionSolar first, Sungrow later).

## Architecture (fixed — do not change without updating docs/DECISIONS.md)
- backend/ Python 3.11 + FastAPI, frontend/ Next.js, DB: PostgreSQL + TimescaleDB
- Vendor integrations ONLY via adapters implementing backend/app/adapters/base.py
- All dashboards read from OUR database, never from vendor APIs directly

## Hard rules
1. NEVER hardcode credentials. Secrets via .env only (.env is gitignored).
   .env.example lists variable NAMES only.
2. FusionSolar Northbound API is strictly rate-limited (~5 calls/10 min/user,
   error 407 on excess, single session). Central scheduler with backoff;
   no ad-hoc API calls from UI code.
3. Every adapter has a mock mode. Develop and test against mocks;
   real API only via scheduler in staging/prod.
4. Every feature: tests first-class (pytest backend). CI must pass before merge.
5. Work in small steps: plan → confirm → implement → test → commit.
   Conventional commits (feat:, fix:, docs:).
6. Never push to main. Feature branches + PR via gh CLI.
7. For any complex/multi-file task: present a plan and wait for approval
   before writing code.
8. Read docs/PRD.md and docs/API-NOTES.md before starting any new module.

## Current phase
Phase 1 — MVP: FusionSolar adapter (mock first), TimescaleDB schema,
ingestion scheduler, basic FastAPI endpoints, plants dashboard page.
