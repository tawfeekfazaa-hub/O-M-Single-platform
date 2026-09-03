# AQ O&M Platform — Product Requirements (Phase 1 MVP)

## 1. Problem

Arabian Qudra Solar operates PV plants monitored through vendor portals
(Huawei FusionSolar today, Sungrow later). Vendor portals are siloed,
rate-limited, and offer no unified O&M workflow. We need a single platform
that owns the data: live + historical monitoring, alarms, IEC 61724-1 KPIs,
and (later) predictive analytics and PM/CM maintenance.

## 2. Goals (Phase 1)

1. **Own the data**: ingest plant + KPI data from FusionSolar into our
   PostgreSQL/TimescaleDB. Dashboards read only from our DB.
2. **Mock-first vendor adapter**: a `FusionSolarAdapter` implementing the
   common `VendorAdapter` interface, fully functional in mock mode so all
   development and CI run without vendor credentials.
3. **Safe ingestion**: one central scheduler is the only component allowed
   to call the vendor API. It respects the ~5 calls / 10 min / user limit
   and backs off on error 407.
4. **Basic API**: FastAPI endpoints for plants, latest KPIs, and KPI history.
5. **Plants dashboard**: a Next.js page listing plants with live status and
   headline KPIs, reading from our API.

### Non-goals (Phase 1)

- Sungrow adapter (interface must allow it; implementation is Phase 2).
- Alarms ingestion/notification pipeline (schema stub only).
- Predictive analytics, PM/CM maintenance module, user auth/roles.

## 3. Users

- **O&M engineers** — watch live plant status, investigate underperformance.
- **Asset managers** — review historical KPIs (PR, yield, availability).

## 4. Functional requirements

| ID  | Requirement |
|-----|-------------|
| F1  | Adapter interface (`backend/app/adapters/base.py`) with: authenticate, list_plants, fetch_plant_kpis, health_check. |
| F2  | FusionSolar adapter with `mock` and `real` modes selected by config; mock generates deterministic plausible data (seeded), incl. a daily power curve. |
| F3  | Rate limiter enforcing N calls per rolling window (default 5/600s) shared across all real API calls in a process. |
| F4  | Ingestion scheduler: periodic cycle → list plants → fetch KPIs → upsert into DB; exponential backoff with jitter on rate-limit/transient errors. |
| F5  | TimescaleDB schema: `plants`, `kpi_measurements` (hypertable), `alarms` (stub). Per-plant isolation via plant_id scoping on every query. |
| F6  | API: `GET /api/v1/health`, `GET /api/v1/plants`, `GET /api/v1/plants/{id}`, `GET /api/v1/plants/{id}/kpis/latest`, `GET /api/v1/plants/{id}/kpis?start=&end=`. |
| F7  | Frontend `/plants` page: table of plants with capacity, status, current power, daily energy; auto-refresh from our API only. |

## 5. KPI definitions (IEC 61724-1 subset, Phase 1)

Stored per plant per timestamp:

- `active_power_kw` — instantaneous AC power.
- `daily_energy_kwh` — energy since local midnight.
- `total_energy_kwh` — lifetime energy.
- `performance_ratio` — vendor-reported PR when available (0–1).

Derived KPIs (specific yield, availability, PR computed from irradiance)
come in a later phase once meteo data is ingested.

## 6. Quality requirements

- pytest suite runs green with zero network access and zero credentials.
- CI (GitHub Actions) runs lint + tests on every PR; must pass before merge.
- No vendor call outside the scheduler path (enforced by code review; the
  API layer has no adapter dependency).
- Secrets only via environment variables (.env locally, never committed).

## 7. Rollout

1. Mock mode everywhere (dev, CI). *(done)*
2. **PR-1** — FusionSolar connector contract validated OFFLINE
   (legacy_system_code profile, pagination, per-endpoint rate budgets,
   normalization, real-mode safety gate). No live vendor call. *(done)*
3. **PR-2** — Raw/Quarantine storage: every real payload lands in raw
   storage first; malformed data is quarantined, never silently ingested.
   Delivered in small steps: **PR-2A0** migration/rollback harness and live
   database CI *(done)*; **PR-2A1** raw schema, provenance and timestamp model;
   **PR-2A2** raw-first capture; **PR-2B** validation, quarantine, idempotent
   promotion and offline replay.
4. Only after PR-2 **and** an approved staging host **and** the company
   data-location policy decision: one controlled live contract-validation
   check (see docs/FUSIONSOLAR-CONTRACT.md "live unknowns"), then
   `FUSIONSOLAR_MODE=real` on staging with the scheduler's conservative
   cadences.
5. Prod: same as staging after a week of stable staging ingestion.
