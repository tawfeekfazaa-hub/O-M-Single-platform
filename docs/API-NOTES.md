# Vendor API Notes

## Huawei FusionSolar Northbound API (openapi)

Reference: iMaster NetEco / FusionSolar "Northbound Interface Reference"
(SmartPVMS). Base URL is region-specific, e.g.
`https://eu5.fusionsolar.huawei.com/thirdData` — configure via
`FUSIONSOLAR_BASE_URL`, never hardcode.

### Authentication & session

- `POST /login` with `userName` + `systemCode` (Northbound account password).
- On success the response carries an `XSRF-TOKEN` cookie/header; every
  subsequent call must send it (`XSRF-TOKEN` header).
- **Single session per user**: logging in again invalidates the previous
  token. Only ONE process (the scheduler) may hold a session.
- Tokens expire (~30 min idle). Re-login on `failCode` 305 (not logged in).

### Rate limiting — THE critical constraint

- ~**5 calls per 10 minutes per user** (varies by tenant/endpoint class).
- Exceeding it returns `failCode` **407** ("access frequency too high").
- Behaviour on 407: back off exponentially (with jitter), do NOT retry
  immediately, do NOT re-login (login calls count against the budget).
- Consequence for design: batch endpoints only (station list, station KPIs
  for many plants per call). Never fan out per-device calls in Phase 1.

### Endpoints used in Phase 1

| Endpoint | Purpose | Notes |
|----------|---------|-------|
| `POST /login` | obtain XSRF token | counts toward budget |
| `POST /getStationList` | list plants (name, code, capacity, address) | paginated |
| `POST /getStationRealKpi` | real-time station KPIs | up to 100 `stationCodes` per call, comma-separated |

Response envelope: `{"success": bool, "failCode": int, "data": ...}`.
`success=false` + `failCode=407` → rate limited; `failCode=305` → re-login.

### Station KPI payload mapping (getStationRealKpi → our model)

| FusionSolar field | Our field |
|-------------------|-----------|
| `dataItemMap.day_power` | `daily_energy_kwh` |
| `dataItemMap.total_power` | `total_energy_kwh` |
| `dataItemMap.real_health_state` | plant status (1 disconnected, 2 faulty, 3 healthy) |
| — (derived/vendor) | `active_power_kw`, `performance_ratio` when exposed |

Field availability differs by tenant/version — the adapter treats every
field as optional and stores NULL when absent.

## Sungrow iSolarCloud (Phase 2 — placeholder)

- OpenAPI with appkey/token auth; different rate limits.
- Must fit the same `VendorAdapter` interface; no schema changes expected
  (vendor-specific fields go to the adapter, not the DB).
