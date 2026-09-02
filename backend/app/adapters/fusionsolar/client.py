"""FusionSolar Northbound API clients — legacy_system_code profile only.

Contract profile (docs/FUSIONSOLAR-CONTRACT.md): userName + systemCode
login yielding an XSRF-TOKEN, ``/thirdData/getStationList`` for the plant
inventory (both documented response variants: legacy direct list and the
paginated ``{list,pageNo,pageSize,pageCount,total}`` envelope on the SAME
path) and ``/thirdData/getStationRealKpi`` for real-time KPIs. There is
deliberately NO fallback to another endpoint and NO OAuth here: a failure
is raised as a typed error with zero additional vendor calls.

Security invariants of this module:
- the XSRF token is only ever attached to requests built from the single
  configured base_url origin (relative paths only, redirects disabled);
- nothing vendor-specific is logged or printed — no headers, cookies,
  tokens, bodies, station identifiers, or KPI values (this module has no
  logging at all by design);
- TLS verification stays at the httpx default (enabled).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import email.utils
import math
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.adapters.base import (
    AdapterAuthError,
    AdapterError,
    AdapterProtocolError,
    AdapterRateLimitError,
    AdapterTransientError,
)
from app.adapters.fusionsolar.policy import KPI_BATCH_SIZE, Endpoint, FusionSolarRatePolicy

# FusionSolar failCodes handled explicitly (docs/FUSIONSOLAR-CONTRACT.md).
FAIL_CODE_NOT_LOGGED_IN = 305
FAIL_CODE_RATE_LIMITED = 407

XSRF_HEADER = "XSRF-TOKEN"

# Vendor-side epoch-milliseconds plausibility window (2001..2096) for
# params.currentTime; anything outside is treated as absent, not guessed.
_MIN_EPOCH_MS = 1_000_000_000_000
_MAX_EPOCH_MS = 4_000_000_000_000

_RETRYABLE_STATUS = {500, 502, 503, 504}

# Plausibility ceiling for a Retry-After hint: one day, the largest window
# any of our endpoint budgets uses. Anything beyond that is not a usable
# instruction — honouring e.g. 1e299 s (a few hundred digits stay FINITE)
# would stop KPI polling for good — so it is treated as a malformed header
# and the endpoint-window fallback applies.
_MAX_RETRY_AFTER_SECONDS = 86_400.0

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)

STATION_LIST_PAGE_SIZE = 100


@dataclass(slots=True)
class StationListResult:
    """Vendor-shaped station rows plus pagination diagnostics (counts only)."""

    stations: list[dict[str, Any]]
    variant: str  # "direct_list" | "paginated"
    pages_retrieved: int
    duplicates_removed: int = 0


@dataclass(slots=True)
class KpiBatchResult:
    """Vendor-shaped KPI rows for one batch plus envelope server time."""

    rows: list[dict[str, Any]]
    vendor_current_time_ms: int | None = None
    calls_consumed: int = 1


@dataclass(slots=True)
class ClientCallCounts:
    """Per-endpoint call counters for diagnostics/tests (counts only)."""

    login: int = 0
    station_list: int = 0
    station_real_kpi: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "login": self.login,
            "station_list": self.station_list,
            "station_real_kpi": self.station_real_kpi,
        }

    def total(self) -> int:
        """Every request actually sent, retries and re-logins included."""
        return self.login + self.station_list + self.station_real_kpi


class FusionSolarClient(Protocol):
    """Vendor-shaped operations shared by the mock and real clients."""

    async def login(self) -> None: ...

    async def list_stations(self) -> StationListResult: ...

    async def get_station_real_kpi(self, station_codes: list[str]) -> KpiBatchResult: ...

    def set_kpi_plant_count(self, plant_count: int) -> None:
        """Tell the client how many plants the caller will request in total.

        The official real-time KPI allowance is ceil(plants/100) calls per
        window, so the batching caller MUST announce the full requested
        count before its first batch — otherwise the real client's budget
        stays at the 1-call constructor default and every batch after the
        first is rejected client-side. A no-op for budget-less clients.
        """

    def is_logged_in(self) -> bool: ...

    def call_counts(self) -> ClientCallCounts: ...

    async def close(self) -> None: ...


def _plausible_delay(seconds: float) -> float | None:
    """A retry hint we can actually act on, else None (use the budget hint).

    Rejects both the overflow case (infinity) and merely absurd finite
    values: ``float("1" * 300)`` is ~1e299 and would freeze the scheduler
    just as effectively as infinity.
    """
    if not math.isfinite(seconds) or seconds > _MAX_RETRY_AFTER_SECONDS:
        return None
    return seconds


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header safely: delta-seconds or HTTP-date."""
    if not value:
        return None
    text = value.strip()
    # RFC 9110 delta-seconds is ASCII digits only. str.isdigit() also accepts
    # superscripts and other numeric obs-text (U+00B2, say), which float()
    # then rejects with a ValueError that would escape the adapter taxonomy;
    # those fall through to the date parser and become a budget hint instead.
    if text.isascii() and text.isdigit():
        return _plausible_delay(float(text))
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: an ASCII HTTP-date whose year does not fit a C long
        # ("Sun, 06 Nov 999999999999999999999999 08:49:37 GMT"). Like any
        # other malformed header it must fall back to the budget hint, not
        # escape the adapter taxonomy and take the scheduler task down.
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        # RFC 9110 requires HTTP-dates in GMT; some servers omit the zone.
        # Assume UTC rather than subtracting a naive from an aware datetime
        # (a TypeError there would escape the adapter error taxonomy and
        # could take the scheduler task down).
        when = when.replace(tzinfo=dt.UTC)
    delta = (when - dt.datetime.now(dt.UTC)).total_seconds()
    return _plausible_delay(max(delta, 0.0)) if math.isfinite(delta) else None


def _station_signature(rows: list[dict[str, Any]]) -> tuple[Any, ...]:
    return tuple(row.get("stationCode") for row in rows)


class RealFusionSolarClient:
    """Talks to the FusionSolar Northbound API over HTTPS (real mode only).

    Session rules: single session per user; at most ONE controlled
    re-login (behind a lock) followed by one retry on failCode 305; NEVER
    a re-login in response to 407/429 — those map to rate-limit errors
    with a lower-bound retry delay from the endpoint's own budget.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        system_code: str,
        policy: FusionSolarRatePolicy,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        max_station_list_pages: int = 50,
        max_total_calls: int | None = None,
    ) -> None:
        self._username = username
        self._system_code = system_code
        self._policy = policy
        self._token: str | None = None
        self._login_lock = asyncio.Lock()
        self._counts = ClientCallCounts()
        self._max_pages = max_station_list_pages
        # Optional absolute ceiling on requests sent by this client instance,
        # enforced at the transport level so that NO path — including the
        # post-305 re-login and its retry, which deliberately bypass the
        # per-endpoint budget — can exceed an advertised hard cap.
        self._max_total_calls = max_total_calls
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            follow_redirects=False,  # redirects are not part of the contract
        )

    # ------------------------------------------------------------------ #
    # transport                                                          #
    # ------------------------------------------------------------------ #

    def is_logged_in(self) -> bool:
        return self._token is not None

    def call_counts(self) -> ClientCallCounts:
        return self._counts

    def set_kpi_plant_count(self, plant_count: int) -> None:
        self._policy.set_kpi_plant_count(plant_count)

    async def _post(
        self,
        endpoint: Endpoint,
        path: str,
        json: dict[str, Any],
        *,
        consume_budget: bool = True,
    ) -> httpx.Response:
        if self._max_total_calls is not None and self._counts.total() >= self._max_total_calls:
            raise AdapterError(
                f"client call cap reached ({self._max_total_calls} requests); "
                "refusing to send another vendor request"
            )
        if consume_budget:
            await self._policy.acquire(endpoint)
        if endpoint is Endpoint.LOGIN:
            self._counts.login += 1
        elif endpoint is Endpoint.STATION_LIST:
            self._counts.station_list += 1
        else:
            self._counts.station_real_kpi += 1

        headers = {XSRF_HEADER: self._token} if self._token else {}
        try:
            return await self._http.post(path, json=json, headers=headers)
        except httpx.DecodingError as exc:
            # A body whose declared content-encoding will not decode (a
            # corrupt gzip response). httpx raises this from post() while
            # reading the response, and it is a RequestError but NOT a
            # TransportError, so the clause below misses it. The bytes are
            # unusable, not a network fault: a protocol error, and above all
            # inside the taxonomy rather than escaping into run_forever().
            raise AdapterProtocolError(
                f"FusionSolar response on {path} could not be decoded: {type(exc).__name__}"
            ) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise AdapterTransientError(
                f"FusionSolar transport failure on {path}: {type(exc).__name__}"
            ) from exc

    def _payload_from(
        self, endpoint: Endpoint, path: str, response: httpx.Response
    ) -> dict[str, Any]:
        status = response.status_code
        if status == 429:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            raise AdapterRateLimitError(
                f"FusionSolar HTTP 429 on {path}",
                retry_after_seconds=retry_after
                if retry_after is not None
                else self._policy.retry_after_hint(endpoint),
            )
        if status in _RETRYABLE_STATUS:
            raise AdapterTransientError(f"FusionSolar HTTP {status} on {path}")
        if status != 200:
            raise AdapterError(f"FusionSolar HTTP {status} on {path}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AdapterProtocolError(f"FusionSolar returned non-JSON body on {path}") from exc
        if not isinstance(payload, dict):
            raise AdapterProtocolError(f"FusionSolar envelope on {path} is not an object")
        return payload

    @staticmethod
    def _fail_code(payload: dict[str, Any]) -> int:
        try:
            return int(payload.get("failCode") or 0)
        except (TypeError, ValueError, OverflowError):
            # OverflowError: JSON 1e309 decodes to infinity, which int()
            # refuses. Like any other unusable failCode it means "not one of
            # the codes we act on" — never an exception outside the adapter
            # taxonomy, which would take the scheduler task down.
            return 0

    async def login(self) -> None:
        """Establish a session. Single-flight: concurrent callers share one login."""
        async with self._login_lock:
            if self._token is not None:
                return
            response = await self._post(
                Endpoint.LOGIN,
                "/login",
                {"userName": self._username, "systemCode": self._system_code},
            )
            payload = self._payload_from(Endpoint.LOGIN, "/login", response)
            if not payload.get("success"):
                fail_code = self._fail_code(payload)
                if fail_code == FAIL_CODE_RATE_LIMITED:
                    raise AdapterRateLimitError(
                        "FusionSolar rate-limited the login call (failCode 407)",
                        retry_after_seconds=self._policy.retry_after_hint(Endpoint.LOGIN),
                    )
                raise AdapterAuthError(f"FusionSolar login failed (failCode={fail_code})")
            # Documented delivery is the XSRF-TOKEN response header; some
            # deployments deliver it as a cookie — accept both.
            token = response.headers.get(XSRF_HEADER) or response.cookies.get(XSRF_HEADER)
            if not token:
                raise AdapterAuthError("FusionSolar login succeeded but returned no XSRF token")
            self._token = token

    async def _call(self, endpoint: Endpoint, path: str, json: dict[str, Any]) -> dict[str, Any]:
        """Authenticated envelope call: 407 -> rate limit; 305 -> at most one
        controlled re-login and one retry; anything malformed -> protocol error.

        Budget accounting: one logical call reserves exactly ONE slot of its
        endpoint's budget. The post-re-login retry after a failCode 305 reuses
        the slot already paid for by the rejected attempt (the re-login itself
        spends the login budget as usual) — otherwise a session expiry on a
        fully-derived budget (e.g. KPI with <=100 plants) could never retry
        and would drop the whole cycle. Worst case this sends one extra HTTP
        request per session expiry (~30 min); whether Huawei counts rejected
        requests against the quota is `unverified` (docs/FUSIONSOLAR-CONTRACT.md).
        """
        if self._token is None:
            await self.login()

        for attempt in (1, 2):
            response = await self._post(endpoint, path, json, consume_budget=attempt == 1)
            payload = self._payload_from(endpoint, path, response)
            if payload.get("success"):
                return payload
            fail_code = self._fail_code(payload)
            if fail_code == FAIL_CODE_RATE_LIMITED:
                raise AdapterRateLimitError(
                    f"FusionSolar failCode 407 on {path} (access frequency too high)",
                    retry_after_seconds=self._policy.retry_after_hint(endpoint),
                )
            if fail_code == FAIL_CODE_NOT_LOGGED_IN and attempt == 1:
                self._token = None
                await self.login()
                continue
            if fail_code == FAIL_CODE_NOT_LOGGED_IN:
                raise AdapterAuthError(
                    "FusionSolar session could not be re-established (repeated failCode 305)"
                )
            raise AdapterError(f"FusionSolar call {path} failed (failCode={fail_code})")
        raise AdapterError(f"FusionSolar call {path} failed")  # pragma: no cover

    # ------------------------------------------------------------------ #
    # station list (/thirdData/getStationList) — both documented variants #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _require_int(data: dict[str, Any], key: str) -> int:
        """Read a MANDATORY non-negative integer field of the paginated envelope.

        Missing or non-integral pagination metadata is a contract violation,
        never something to guess a default for: a wrong assumption here can
        turn a truncated inventory into an apparently complete one.
        """
        raw = data.get(key)
        if raw is None:
            raise AdapterProtocolError(f"paginated station list is missing {key}")
        if isinstance(raw, bool) or isinstance(raw, float) and not raw.is_integer():
            raise AdapterProtocolError(f"paginated station list has non-integer {key}")
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise AdapterProtocolError(f"paginated station list has non-numeric {key}") from exc
        if value < 0:
            raise AdapterProtocolError(f"paginated station list has negative {key}")
        return value

    def _parse_page_count(self, data: dict[str, Any]) -> int:
        count = self._require_int(data, "pageCount")
        if count > self._max_pages:
            # Fail on page 1 (one call) instead of burning the whole budget
            # and dying part-way. The guard is min(configured pages, the
            # station-list budget): a refresh must fit in ONE window.
            raise AdapterProtocolError(
                f"station list needs {count} pages but the effective guard is "
                f"{self._max_pages}; raise FUSIONSOLAR_STATION_LIST_MAX_CALLS "
                "(and _MAX_PAGES) to retrieve this inventory"
            )
        return count

    async def list_stations(self) -> StationListResult:
        stations: list[dict[str, Any]] = []
        seen: dict[str, dict[str, Any]] = {}
        duplicates_removed = 0
        variant: str | None = None
        previous_signature: tuple[Any, ...] | None = None
        page_no = 1
        page_count: int | None = None
        page_size: int | None = None
        total: int | None = None

        while True:
            if page_no > self._max_pages:
                raise AdapterProtocolError(
                    f"station list exceeded the {self._max_pages}-page guard"
                )
            payload = await self._call(
                Endpoint.STATION_LIST,
                "/getStationList",
                {"pageNo": page_no, "pageSize": STATION_LIST_PAGE_SIZE},
            )
            data = payload.get("data")

            if isinstance(data, list):
                # Legacy variant: the whole inventory in one direct list.
                if page_no != 1:
                    raise AdapterProtocolError(
                        "station list switched to direct-list variant mid-pagination"
                    )
                variant = "direct_list"
                rows = data
                page_count = 1
            elif isinstance(data, dict):
                variant = "paginated"
                rows = data.get("list")
                if not isinstance(rows, list):
                    raise AdapterProtocolError("paginated station list has no 'list' array")
                # Strict envelope contract: pageNo/pageSize/pageCount/total
                # must all be present, well-formed and stable across pages.
                # Incomplete or contradictory metadata is rejected instead of
                # defaulted, because a truncated inventory that passes as
                # complete would silently retire live plants downstream.
                echoed_page_no = self._require_int(data, "pageNo")
                if echoed_page_no != page_no:
                    raise AdapterProtocolError(
                        f"station list echoed pageNo={echoed_page_no} for requested page {page_no}"
                    )
                echoed_page_size = self._require_int(data, "pageSize")
                if echoed_page_size < 1:
                    raise AdapterProtocolError("station list pageSize must be >= 1")
                if len(rows) > echoed_page_size:
                    raise AdapterProtocolError("station list page holds more rows than pageSize")
                echoed_page_count = self._parse_page_count(data)
                echoed_total = self._require_int(data, "total")
                if page_no == 1:
                    page_count = echoed_page_count
                    page_size = echoed_page_size
                    total = echoed_total
                    # Page 1 is the first moment the burst size is known.
                    # Starting a burst the budget cannot finish spends
                    # calls on an inventory that is never retrieved, and
                    # leaves nothing for the retry: a fleet that grew a
                    # page since the last refresh would take the free
                    # slots and be rejected on its last page. Stop here
                    # instead, with the wait the scheduler needs.
                    pages_needed = page_count or 0
                    remaining = max(pages_needed - 1, 0)
                    if self._policy.wait_for_slots(Endpoint.STATION_LIST, remaining) > 0:
                        # The wait is measured for the WHOLE next attempt,
                        # which restarts at page 1 and therefore needs every
                        # page again — page 1's own call included, since it
                        # keeps occupying its slot until it expires. Asking
                        # only for the pages still missing here would hand
                        # back a delay that lets the retry spend the one slot
                        # that just freed and stop at the same place, over
                        # and over, never refreshing at all.
                        raise AdapterRateLimitError(
                            "station list needs more pages than the station-list "
                            "budget has free; deferring the whole refresh",
                            retry_after_seconds=self._policy.wait_for_slots(
                                Endpoint.STATION_LIST, pages_needed
                            ),
                            retry_after_covers_whole_attempt=True,
                        )
                elif echoed_page_count != page_count:
                    # The FIRST page's metadata is authoritative: values that
                    # change mid-pagination could end the loop early.
                    raise AdapterProtocolError("station list pageCount changed during pagination")
                elif echoed_page_size != page_size:
                    raise AdapterProtocolError("station list pageSize changed during pagination")
                elif echoed_total != total:
                    raise AdapterProtocolError("station list total changed during pagination")
                if echoed_page_count == 0 and (rows or echoed_total):
                    # Zero pages is only coherent for an empty fleet.
                    raise AdapterProtocolError(
                        "station list reported pageCount=0 with a non-empty inventory"
                    )
                if rows == [] and (echoed_total > 0 or page_no < (page_count or 0)):
                    # An empty page is only coherent for an empty fleet. A
                    # terminal empty page would otherwise be certified as
                    # complete while wasting a station-list call and
                    # inflating pages_retrieved, which stretches the next
                    # refresh (2 pages -> 12 h instead of 6 h).
                    raise AdapterProtocolError(
                        "station list returned an empty page in a non-empty inventory"
                    )
            else:
                raise AdapterProtocolError("station list data is neither a list nor an object")

            for row in rows:
                if not isinstance(row, dict):
                    raise AdapterProtocolError("station list row is not an object")
                code = row.get("stationCode")
                if not code:
                    raise AdapterProtocolError("station list row lacks stationCode")
                code = str(code)
                if code in seen:
                    if seen[code] == row:
                        duplicates_removed += 1
                        continue
                    raise AdapterProtocolError(
                        "station list contains conflicting duplicate stationCode entries"
                    )
                seen[code] = row
                stations.append(row)

            signature = _station_signature(rows)
            if previous_signature is not None and signature == previous_signature and rows:
                raise AdapterProtocolError("station list repeated an identical page")
            previous_signature = signature

            if page_count is None or page_no >= page_count:
                break
            page_no += 1

        if total is not None and len(stations) != total:
            # Counts only — never identifiers. A short (or long) inventory is
            # a failed retrieval, not a smaller plant fleet.
            raise AdapterProtocolError(
                f"station list returned {len(stations)} unique stations "
                f"but the envelope reported total={total}"
            )

        return StationListResult(
            stations=stations,
            variant=variant or "direct_list",
            pages_retrieved=page_no,
            duplicates_removed=duplicates_removed,
        )

    # ------------------------------------------------------------------ #
    # real-time KPIs (/thirdData/getStationRealKpi)                      #
    # ------------------------------------------------------------------ #

    async def get_station_real_kpi(self, station_codes: list[str]) -> KpiBatchResult:
        if not station_codes:
            return KpiBatchResult(rows=[], calls_consumed=0)
        if len(station_codes) > KPI_BATCH_SIZE:
            raise AdapterError(
                f"getStationRealKpi accepts at most {KPI_BATCH_SIZE} station codes per call"
            )
        payload = await self._call(
            Endpoint.STATION_REAL_KPI,
            "/getStationRealKpi",
            {"stationCodes": ",".join(station_codes)},
        )
        data = payload.get("data")
        if not isinstance(data, list):
            raise AdapterProtocolError("getStationRealKpi data is not a list")
        for row in data:
            if not isinstance(row, dict):
                raise AdapterProtocolError("getStationRealKpi row is not an object")

        vendor_ms: int | None = None
        params = payload.get("params")
        if isinstance(params, dict):
            raw = params.get("currentTime")
            if isinstance(raw, int | float) and _MIN_EPOCH_MS <= raw <= _MAX_EPOCH_MS:
                vendor_ms = int(raw)
        return KpiBatchResult(rows=data, vendor_current_time_ms=vendor_ms)

    async def close(self) -> None:
        await self._http.aclose()
