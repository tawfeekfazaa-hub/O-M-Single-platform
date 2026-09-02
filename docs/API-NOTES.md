# Vendor API Notes

## Huawei FusionSolar Northbound API (legacy_system_code profile)

**The normative contract lives in docs/FUSIONSOLAR-CONTRACT.md** (with
the Huawei source URLs, verification levels, and live unknowns). This
file is the quick operational summary.

Base URL is region-specific (your portal host + `/thirdData`) —
configured via `FUSIONSOLAR_BASE_URL`, HTTPS only, never hardcoded.

### Authentication & session

- `POST /login` with `userName` + `systemCode`
  (env: `FUSIONSOLAR_USERNAME` + `FUSIONSOLAR_SYSTEM_CODE`; the
  systemCode is a dedicated API credential, not a portal password).
- `XSRF-TOKEN` comes back in the response header (some deployments: as a
  cookie); it must accompany every subsequent call, and is only ever sent
  to the configured origin.
- **Single session per user** — only ONE process (the scheduler) may hold
  a session. Token validity ≈ 30 min; `failCode 305` → at most one
  controlled re-login + one retry.

### Rate limiting — per endpoint, NOT one global budget

The old "~5 calls / 10 min for everything" description was wrong; Huawei
limits each endpoint class separately:

| Endpoint | Official limit | Our client-side budget |
|----------|----------------|------------------------|
| `POST /login` | 5 / 10 min per user (also: 5 wrong passwords → 30-min lock) | 4 / 600 s (margin) |
| `POST /getStationList` | small daily-style allowance; exact formula varies by SmartPVMS version | **safety default** 4 / day; inventory cadence 6 h, stretched to pages × window / budget for paginated inventories (2 pages → 12 h) |
| `POST /getStationRealKpi` | ceil(plants/100) / 5 min, ≤100 codes per call | derived at runtime from plant count |

`failCode 407` **or HTTP 429** = frequency exceeded → back off (jitter),
never retry immediately, never re-login in response. All vendor calls are
sequential. Exhausting one budget never spends another.

### Endpoints used in Phase 1

| Endpoint | Purpose | Notes |
|----------|---------|-------|
| `POST /login` | obtain XSRF token | counts toward the login budget |
| `POST /getStationList` | plant inventory | `pageNo`/`pageSize=100`; BOTH documented variants parsed on this path (direct list and `{list,pageNo,pageSize,pageCount,total}`); the paginated envelope is validated strictly (all four fields mandatory, stable across pages, final count == `total`) and a failed validation keeps the previous inventory; pages per refresh are bounded by the daily budget; refresh spacing is derived from the budget slots consumed — one per logical page, so the failCode 305 retry does not inflate it — never every KPI cycle |
| `POST /getStationRealKpi` | real-time station KPIs | ≤100 `stationCodes` per sequential batch |

`/thirdData/stations` + OAuth are a documented FUTURE upgrade path
(newer SmartPVMS): out of scope, no code, **no auto-fallback**.

Response envelope: `{"success": bool, "failCode": int, "data": ...,
"params": {"currentTime": ms, ...}}`. `params.currentTime` is vendor
SERVER time (not a device measurement timestamp).

### Station KPI payload mapping (getStationRealKpi → our model)

| FusionSolar field | Our field | Notes |
|-------------------|-----------|-------|
| `dataItemMap.day_power` | `daily_energy_kwh` | kWh, finite-validated |
| `dataItemMap.total_power` | `total_energy_kwh` | kWh, finite-validated |
| `dataItemMap.real_health_state` | plant status | 1 disconnected, 2 faulty, 3 healthy, else unknown |
| `dataItemMap.performance_ratio` | `performance_ratio` | tenant-dependent; normalized to 0..1 (89 → 0.89) |
| — | `active_power_kw` | **no documented station-level field → None in real mode** (mock's `real_power` is synthetic) |
| `params.currentTime` | `vendor_server_time` | vendor server clock — on the in-flight reading only; persisted from PR-2 (Raw/Quarantine) |

Field availability differs by tenant/version — every field is optional
and invalid values (NaN/∞/impossible) are rejected, not stored.

## Sungrow iSolarCloud (Phase 2 — placeholder)

- OpenAPI with appkey/token auth; different rate limits.
- Must fit the same `VendorAdapter` interface; the generic rate-limiter
  and per-endpoint policy pattern are reusable.
