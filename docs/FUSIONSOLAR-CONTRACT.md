# FusionSolar Northbound Contract (PR-1, legacy_system_code profile)

Status: implemented offline in PR-1. **No live Huawei request has been
made.** Every item below is tagged with its verification level.

## Normative references (Huawei primary documentation)

Huawei's primary documentation is the ONLY normative source for this
contract. Community clients are cited further down strictly as
non-normative implementation comparisons; no production behavior, endpoint
choice, unit conversion, or fallback is based solely on community code.

| # | Document | URL |
|---|----------|-----|
| 1 | Plant List Interface | https://support.huawei.com/enterprise/en/doc/EDOC1100332812/67aab0ee/plant-list-interface |
| 2 | Plant Data Interfaces | https://support.huawei.com/enterprise/en/doc/EDOC1100316813/cf4fb068/plant-data-interfaces |
| 3 | Login Interface | https://support.huawei.com/enterprise/en/doc/EDOC1100332812/9e1a18d2/login-interface |
| 4 | Error Code List | https://support.huawei.com/enterprise/en/doc/EDOC1100492747/97234b41/error-code-list |
| 5 | Why 407 or 429 | https://support.huawei.com/enterprise/en/doc/EDOC1100492747/8f551cf3/why-does-the-northbound-api-return-error-code-407-or-429 |
| 6 | 26.2 best practices (zh) | https://support.huawei.com/enterprise/zh/doc/EDOC1100589816/c68a431b/best-practices |

**Access record (2026-09-01):** direct access to `support.huawei.com` was
blocked by the network egress proxy of the execution environment (HTTP
403 on CONNECT). Official page titles and excerpts WERE available through
web search and were used together with the documents' known structure.
Non-normative implementation comparisons consulted:
[tijsverkoyen/HomeAssistant-FusionSolar](https://github.com/tijsverkoyen/HomeAssistant-FusionSolar)
and [EnergieID/FusionSolar](https://github.com/EnergieID/FusionSolar).
Items that could not be pinned to primary text are explicitly marked
`unverified` or `requires-live-contract-validation` below.

## API profile matrix

| Profile | Auth | Endpoints | Status |
|---------|------|-----------|--------|
| **legacy_system_code** (this repo) | `userName` + `systemCode` → `XSRF-TOKEN` | `/thirdData/login`, `/thirdData/getStationList`, `/thirdData/getStationRealKpi` | Implemented (offline) |
| oauth / `/thirdData/stations` | OAuth / access token (newer SmartPVMS docs) | `/thirdData/stations` (paginated) | **Documented future upgrade path — out of scope, zero code** |

There is **no auto-fallback** in either direction and no probing of
alternative endpoints on failure: a failure raises a typed error with
zero additional vendor calls (extra calls would burn rate budget and can
mask authentication/version errors).

## Login — confirmed

- `POST /thirdData/login` with body `{userName, systemCode}`.
- `systemCode` is a dedicated Northbound API credential (canonical env
  var `FUSIONSOLAR_SYSTEM_CODE`; `FUSIONSOLAR_PASSWORD` is a deprecated
  alias), not an ordinary portal password.
- The XSRF token is returned in the `XSRF-TOKEN` response header
  (documented); real-world deployments also deliver it as a cookie — we
  accept both. *Which one your tenant uses:*
  `requires-live-contract-validation`.
- Token validity ≈ 30 minutes; `failCode 305` = not logged in → at most
  ONE controlled re-login + one retry (single-flight behind a lock).
- Login traffic limit: **5 calls / 10 minutes per user (official)**; five
  wrong passwords within 10 minutes lock the account for 30 minutes.
  Our client-side budget: 4/600 s (official minus safety margin).

## Station list — `/thirdData/getStationList`

- The request carries `pageNo` (from 1) and `pageSize=100`.
- Two **documented contract variants on this same path** are parsed:
  1. legacy direct list — `data` is the full station array (older
     versions ignore the pagination parameters); `confirmed`;
  2. paginated envelope — `data = {list, pageNo, pageSize, pageCount,
     total}`; `documented version difference` (which variant your tenant
     serves: `requires-live-contract-validation`).
- Station fields used: `stationCode`, `stationName`, `stationAddr`,
  `capacity` (**MW**, stored as kWp ×1000 — unit `confirmed` for the
  legacy variant; for a paginated tenant: `requires-live-contract-validation`).
- **Strict paginated-envelope contract.** `pageNo`, `pageSize`,
  `pageCount` and `total` are all MANDATORY and validated on every page:
  the echoed `pageNo` must equal the requested page; `pageSize` must be
  >= 1 and never smaller than the rows delivered; the FIRST page's
  `pageCount`/`pageSize`/`total` are authoritative and any change on a
  later page is rejected; and at the end the number of unique stations
  must equal `total`. Missing or contradictory metadata raises
  `AdapterProtocolError` — nothing is defaulted or guessed, because a
  truncated inventory that passed as complete would retire live plants
  downstream.
- Guards: every page retrieved; finite max-page bound; repeated-page and
  impossible-metadata detection; deterministic `stationCode` dedup (the
  unique count, not the row count, is what `total` is checked against);
  conflicting duplicates rejected; malformed pages never skipped silently.
  An EMPTY page is coherent only for an empty fleet (`total = 0`) — an
  empty page anywhere in a non-empty inventory, terminal one included, is
  rejected: accepting it would certify a contradictory envelope, waste a
  station-list call, and inflate `pages_retrieved`, which stretches the
  next refresh (2 pages → 12 h instead of 6 h).
- A failed validation means **no inventory update at all**: the
  repository keeps the previously stored plants, the refresh is never
  reported as successful, and the cycle is never a complete success (see
  the deferral rule below for what happens next).
- The legacy direct-list variant is unchanged: one call, complete by
  definition, no pagination metadata expected.
- Budget: Huawei documents a small daily-style allowance whose exact
  formula **varies by SmartPVMS version** (one published form:
  `roundup(plants/100) × 10 + 24` per day — treated as
  `documented version difference`, NOT a universal constant). Our
  client-side budget of **4 calls/day** and the **6-hour inventory
  cadence are SAFETY DEFAULTS**, both configurable.
- Every configured budget, window and cadence must be **finite and > 0**
  (the KPI margin may be 0), and the pre-flight check rejects anything else
  by NAME. A non-finite value is not merely odd: a NaN inventory cadence
  makes the elapsed-time comparison never true, so the inventory is
  refreshed once and never again — new and retired stations go unnoticed
  indefinitely — and a non-finite poll interval is slept on directly and
  stalls the loop.
- Cadence vs pagination: each page spends one station-list call, and the
  budget is a ROLLING window, so only a whole number of complete refreshes
  fits it. The scheduler spaces refreshes by
  `window / floor(budget / pages)` — never an average rate, which would
  drift into the window (a 5-call budget with 2-page refreshes would put
  bursts at 0 h, 9.6 h and 19.2 h, needing six slots in one window). On the
  4/day default: 1 page → 6 h, 2 pages → 12 h, 3–4 pages → 24 h.
  `pages` here means BUDGET SLOTS, not HTTP attempts: the retry after a
  failCode 305 reuses the slot its rejected attempt already paid for, so it
  raises the transport counter (kept for diagnostics) without costing
  budget. Pacing from the transport counter would read a one-page refresh
  as a two-slot burst and stretch the next refresh from 6 h to 12 h.
- KPI polling covers the LAST SUCCESSFUL inventory only. Phase-1
  persistence has no delete, so a station the vendor drops keeps its
  repository row; polling it would mark every later cycle partial and
  waste KPI capacity on a station the vendor will never answer for.
  **Restart gap (accepted, tracked for PR-2):** the inventory snapshot
  lives in the process, so after a restart — before the first successful
  station-list refresh — the persisted plants are polled *provisionally*
  and the cycle is flagged `inventory_provisional`. Blocking KPI polling
  until a snapshot exists was rejected: a station-list budget that is
  already spent would then black out monitoring of every real plant for up
  to a full window, which is far worse than briefly polling a retired one.
  The set self-corrects at the first successful refresh; PR-2 persists the
  snapshot and closes the gap for good.
- Staleness is a STATE, not an event: once a refresh is rate-limited or
  fails, every cycle of the deferral window reports it (the cycle runs on
  the same old list), is excluded from `complete_success` and is counted
  in the incomplete statistic. Only a successful refresh clears it.
- A refresh rejected by the budget never aborts KPI polling, and defers
  itself by a **full window** rather than the limiter's next-slot hint:
  one freed slot is not enough for a paginated refresh, so retrying earlier
  would resend the same partial burst and fail on the same page forever.
- An inventory refresh that FAILS (contract/guard/vendor error, not just a
  rate limit) is deferred like a rate-limited one instead of aborting the
  cycle: retrying it every cycle would spend page 1 of the budget until the
  window is exhausted, and aborting would stop KPI monitoring with it. The
  failure is recorded on the cycle (`inventory_error`) and the cycle is
  never a complete success while it stands.
- **Pages per refresh may never exceed the budget.** A paginated refresh
  cannot be resumed across windows, so the effective page guard is
  `min(FUSIONSOLAR_STATION_LIST_MAX_PAGES, FUSIONSOLAR_STATION_LIST_MAX_CALLS)`.
  A larger inventory fails on page 1 (one call) with an actionable message
  instead of burning the whole budget and dying part-way; retrieving an
  N-page inventory requires a station-list budget of at least N.

## Real-time KPIs — `/thirdData/getStationRealKpi`

- `stationCodes` comma-separated, **max 100 per call (official)**;
  batches are sequential, never concurrent. Each returned row is validated
  against the codes of ITS OWN batch — a row for a station belonging to a
  later batch is misrouted data, counted as unexpected and dropped, so a
  stale value can never displace the station's real row.
- Allowance: **ceil(plants/100) calls per 5 minutes (official)** —
  derived at runtime from the plant count.
- Documented `dataItemMap` fields: `day_power`, `month_power`,
  `total_power`, `day_income`, `total_income`, `real_health_state`
  (`confirmed`). `day_power`/`total_power` are kWh and stay kWh.
- `real_health_state`: 1 disconnected, 2 faulty, 3 healthy, else unknown.
  An ABSENT field and a numeric code outside 1/2/3 are both the documented
  "else unknown" case and are not counted as invalid; a value that is
  present but unreadable (bool, text, NaN, ∞, or a FRACTIONAL number —
  never truncated, since `3.7` would read as healthy and `1.5` as
  disconnected) IS counted, so a malformed response can never be reported
  as a complete ingestion. An integral JSON number such as `3.0` is the
  integer code 3 and stays valid.
- **Station-level active power: NOT in the documented contract.** The
  real adapter stores `None` and never derives it from another field.
  (Device-level active power belongs to the device interfaces — later
  phase.) The mock's `real_power` is synthetic mock-only data.
- `performance_ratio` here is `tenant/version-dependent` (documented in
  daily-KPI interfaces): when present it is normalized to 0..1
  (percent-style `89` → `0.89`; an already-normalized 0..1 value is an
  explicitly tested compatibility case; negative/NaN/∞/>100 rejected).
- NaN/∞ are rejected for every numeric field.
- Envelope `params.currentTime` (epoch ms) is carried on the in-flight
  reading as **vendor server time** — it is NOT a device measurement
  timestamp — next to the local received-at time (`ts`). The persisted
  KPI schema does not store it yet: durable retention (with the full raw
  envelope) arrives with the PR-2 Raw/Quarantine layer.

## Errors & transport

- `failCode 407` → rate-limit error with the endpoint window as a
  lower-bound retry delay; **never** an immediate retry, **never** a
  re-login (login budget is separate and precious).
- `failCode 305` retry accounting: one logical call reserves exactly ONE
  slot of its endpoint's budget; the single post-re-login retry reuses
  the slot paid for by the rejected attempt (the re-login spends the
  login budget as usual). Worst case this sends one extra HTTP request
  per session expiry (≈30 min); *whether Huawei counts rejected requests
  against the endpoint quota:* `unverified`.
- HTTP `429` → rate-limit error; `Retry-After` parsed safely
  (delta-seconds or HTTP-date; a timezone-less date is read as GMT per
  RFC 9110). A hint is only accepted when it is FINITE and within a
  plausibility ceiling of **one day** — the largest window our budgets
  use; anything else (garbage, infinity from an overflowing digit string,
  or a merely absurd finite value such as 1e299, which Python does not
  overflow, or an HTTP-date whose year or offset overflows a C long and
  makes the stdlib parser raise OverflowError) falls back to the
  endpoint-window hint. delta-seconds is read as **ASCII digits only**, per
  RFC 9110: Python's `str.isdigit()` also accepts obs-text digits such as
  superscripts, which `float()` then rejects. Parsing never raises
  outside the adapter error taxonomy and can never yield a delay that
  stalls the scheduler. *Whether your tenant sends Retry-After:*
  `unverified`.
- Timeouts, connection failures, 500/502/503/504 → transient error →
  scheduler backoff with jitter (no blind retries). Jitter (0.75x–1.25x)
  applies to the BACKOFF only: a vendor `Retry-After` and the configured
  minimum interval are hard lower bounds applied afterwards, so the next
  request is never sent before the server's requested delay.
- Non-JSON bodies, non-object envelopes, contract-violating shapes →
  protocol error.
- HTTPS only; TLS verification enabled; redirects disabled; explicit
  connect/read/write/pool timeouts; the token goes only to the single
  configured origin. Nothing vendor-specific is ever logged.

## Items requiring live contract validation (PR-2 controlled check)

1. Which station-list variant (direct vs paginated) the tenant serves.
2. `capacity` unit under the paginated variant.
3. XSRF token delivery (header vs cookie) on this tenant.
4. Presence/units of `performance_ratio` in `getStationRealKpi`.
5. Presence of `Retry-After` on HTTP 429.
6. The exact daily station-list allowance for this account/version.

The controlled live validation itself remains prohibited until PR-2
Raw/Quarantine storage, an approved staging host, and the company
data-location policy decision.
