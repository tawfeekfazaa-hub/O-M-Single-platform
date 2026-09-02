"""Central ingestion scheduler.

The ONLY component allowed to call vendor adapters (CLAUDE.md rule 2).

Two independent cadences (docs/FUSIONSOLAR-CONTRACT.md):
- station INVENTORY refresh — conservative (default 6 h): the vendor's
  station-list budget is tiny, so it must NOT be called on every cycle.
  A paginated inventory consumes one station-list call PER PAGE, so when
  the daily budget is known the effective spacing between refreshes is
  stretched to pages x window / budget (a 6 h cadence is only sustainable
  for a one-page inventory on the 4/day safety default). A rate-limited
  refresh defers itself and never aborts KPI polling for the cycle;
- real-time KPI polling — every cycle, reading the plant list from the
  repository cache and fetching KPIs in sequential batches.

On rate-limit or transient vendor errors the scheduler backs off
exponentially with jitter instead of hammering the API. Clock/sleep/
jitter are injectable so tests run instantly. Cycle diagnostics carry
counts only — never plant identifiers, names, or KPI values.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.adapters.base import (
    AdapterError,
    AdapterRateLimitError,
    AdapterTransientError,
    VendorAdapter,
)
from app.repositories.base import Repository

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CycleResult:
    """Counts-only outcome of one scheduler cycle."""

    inventory_refreshed: bool = False
    inventory_pages: int = 0
    inventory_rate_limited: bool = False
    plants_upserted: int = 0
    requested_plants: int = 0
    readings_returned: int = 0
    readings_written: int = 0
    readings_missing: int = 0
    readings_duplicate: int = 0
    readings_unexpected: int = 0
    invalid_values: int = 0
    batches: int = 0
    calls_consumed: int = 0
    rate_limited: bool = False
    retry_after_seconds: float | None = None
    transient: bool = False
    error: str | None = None

    @property
    def partial(self) -> bool:
        """True when the vendor answered but the data set was not whole."""
        return (
            self.readings_missing > 0
            or self.readings_duplicate > 0
            or self.readings_unexpected > 0
            or self.invalid_values > 0
        )

    @property
    def complete_success(self) -> bool:
        """A cycle counts as fully successful ONLY with no error and no
        partial/malformed data — a partial response is never reported as
        a complete ingestion."""
        return self.error is None and not self.partial


@dataclass(slots=True)
class SchedulerStats:
    cycles_total: int = 0
    cycles_failed: int = 0
    cycles_partial: int = 0
    consecutive_failures: int = 0
    last_result: CycleResult | None = field(default=None)


class IngestionScheduler:
    def __init__(
        self,
        adapter: VendorAdapter,
        repository: Repository,
        *,
        interval_seconds: float = 300.0,
        min_interval_seconds: float = 0.0,
        inventory_refresh_seconds: float = 21_600.0,
        station_list_max_calls: int | None = None,
        station_list_window_seconds: float | None = None,
        backoff_base_seconds: float = 60.0,
        backoff_max_seconds: float = 1800.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._adapter = adapter
        self._repository = repository
        # min_interval lets real-mode wiring enforce the KPI window+margin.
        # It is a floor on EVERY delay, not just the success interval: after
        # a failed cycle a shorter backoff would just hit the client-side
        # KPI limiter again and manufacture another failure.
        self._min_interval = min_interval_seconds
        self._interval = max(interval_seconds, min_interval_seconds)
        self._inventory_refresh = inventory_refresh_seconds
        # Station-list budget (when wired) stretches the effective refresh
        # spacing for paginated inventories; None -> no derived spacing.
        self._station_list_max_calls = station_list_max_calls
        self._station_list_window = station_list_window_seconds
        self._inventory_min_spacing = 0.0
        self._inventory_not_before: float | None = None
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds
        self._sleep = sleep
        self._jitter = jitter
        self._clock = clock
        self._last_inventory_at: float | None = None
        # vendor_plant_ids of the last SUCCESSFUL inventory refresh. Plants
        # the vendor has dropped stay in the repository (no delete in the
        # Phase-1 schema), so polling the repository blindly would request
        # KPIs for retired stations forever — every cycle partial, and KPI
        # capacity wasted on rows the vendor will never answer for.
        self._current_inventory: set[str] | None = None
        self.stats = SchedulerStats()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def interval_seconds(self) -> float:
        return self._interval

    def _inventory_due(self) -> bool:
        now = self._clock()
        if self._inventory_not_before is not None and now < self._inventory_not_before:
            return False
        if self._last_inventory_at is None:
            return True
        spacing = max(self._inventory_refresh, self._inventory_min_spacing)
        return (now - self._last_inventory_at) >= spacing

    async def _refresh_inventory(self, result: CycleResult) -> None:
        plants = await self._adapter.list_plants()
        await self._repository.upsert_plants(plants)
        self._last_inventory_at = self._clock()
        self._inventory_not_before = None
        result.inventory_refreshed = True
        result.plants_upserted = len(plants)
        self._current_inventory = {p.vendor_plant_id for p in plants}
        calls = 1
        diag = getattr(self._adapter, "last_inventory_diagnostics", None)
        if diag is not None:
            result.inventory_pages = diag.pages_retrieved
            result.calls_consumed += diag.calls_consumed
            calls = max(diag.calls_consumed, 1)
        if self._station_list_max_calls and self._station_list_window:
            self._inventory_min_spacing = self._derive_inventory_spacing(calls)

    def _derive_inventory_spacing(self, calls: int) -> float:
        """Spacing that lets EVERY refresh spend all its pages at once.

        The budget is a rolling window, not an average allowance: the
        previous refreshes' calls keep occupying slots until they age out.
        What fits is therefore a whole number of complete bursts —
        ``floor(budget / calls)`` of them per window — so the spacing is
        ``window / that count``. An average-rate formula
        (``window * calls / budget``) looks safe for two bursts but drifts:
        with a 5-call window and 2-page refreshes it would schedule bursts
        at 0 h, 9.6 h and 19.2 h, needing six slots inside one window.
        """
        window = self._station_list_window or 0.0
        budget = self._station_list_max_calls or 1
        bursts_per_window = budget // max(calls, 1)
        if bursts_per_window < 1:
            # One refresh does not even fit the budget: the guard in the
            # adapter factory rejects this, but never schedule faster than
            # a full window here either.
            return window
        return window / bursts_per_window

    async def run_cycle(self) -> CycleResult:
        """One ingestion pass. Never raises — errors land in the result."""
        result = CycleResult()
        try:
            await self._adapter.authenticate()

            # Inventory on its own conservative cadence — never every cycle.
            if self._inventory_due():
                try:
                    await self._refresh_inventory(result)
                except AdapterRateLimitError as exc:
                    # The station-list budget is independent of the KPI
                    # budget: a rate-limited refresh defers itself and must
                    # never abort KPI polling for this cycle.
                    result.inventory_rate_limited = True
                    # The limiter's hint frees ONE slot, which is not enough
                    # for a paginated refresh: retrying then would spend the
                    # same partial burst again and fail on the same page,
                    # forever. Wait a full window so every call of the failed
                    # burst has expired before the next attempt.
                    deferral = exc.retry_after_seconds or self._inventory_refresh
                    if self._station_list_window:
                        deferral = max(deferral, self._station_list_window)
                    self._inventory_not_before = self._clock() + deferral
                    logger.warning(
                        "inventory refresh rate-limited, deferred; KPI polling continues"
                    )

            # KPI polling uses the repository inventory, not a vendor call.
            plants = [
                p for p in await self._repository.list_plants() if p.vendor == self._adapter.vendor
            ]
            codes = [p.vendor_plant_id for p in plants]
            if self._current_inventory is not None:
                # Restrict to the last inventory the vendor actually served.
                codes = [c for c in codes if c in self._current_inventory]
            result.requested_plants = len(codes)
            if codes:
                readings = await self._adapter.fetch_plant_kpis(codes)
                result.readings_returned = len(readings)
                result.readings_written = await self._repository.record_kpis(readings)
                diag = getattr(self._adapter, "last_kpi_diagnostics", None)
                if diag is not None:
                    result.readings_missing = diag.missing
                    result.readings_duplicate = diag.duplicates
                    result.readings_unexpected = diag.unexpected
                    result.invalid_values = diag.invalid_values
                    result.batches = diag.batches
                    result.calls_consumed += diag.calls_consumed
        except AdapterRateLimitError as exc:
            result.rate_limited = True
            result.error = str(exc)
            result.retry_after_seconds = exc.retry_after_seconds
            logger.warning("ingestion rate-limited: %s", exc)
        except AdapterTransientError as exc:
            result.transient = True
            result.error = str(exc)
            logger.warning("ingestion transient failure: %s", exc)
        except AdapterError as exc:
            result.error = str(exc)
            logger.error("ingestion cycle failed: %s", exc)

        self.stats.cycles_total += 1
        if result.error is None:
            self.stats.consecutive_failures = 0
        else:
            self.stats.cycles_failed += 1
            self.stats.consecutive_failures += 1
        if result.error is None and result.partial:
            self.stats.cycles_partial += 1
            # Counts only — no identifiers or values in this log line.
            logger.warning(
                "ingestion cycle partial: requested=%d returned=%d missing=%d "
                "duplicate=%d unexpected=%d invalid=%d",
                result.requested_plants,
                result.readings_returned,
                result.readings_missing,
                result.readings_duplicate,
                result.readings_unexpected,
                result.invalid_values,
            )
        self.stats.last_result = result
        return result

    def next_delay(self, result: CycleResult) -> float:
        """Normal interval on success; exponential backoff + jitter on failure.

        Jitter is applied to the BACKOFF only. A vendor Retry-After (or a
        budget hint) is a hard lower bound: scaling it by 0.75 would send the
        next request before the server's requested delay and earn another
        429. The same holds for the configured minimum interval.
        """
        if result.error is None:
            return self._interval
        exponent = min(self.stats.consecutive_failures - 1, 5)
        delay = min(self._backoff_max, self._backoff_base * (2**exponent))
        # 0.75x..1.25x jitter so multiple deployments don't sync up.
        delay *= 0.75 + 0.5 * self._jitter()
        # Hard lower bounds, applied AFTER jitter so they are never undercut.
        if result.retry_after_seconds is not None:
            delay = max(delay, result.retry_after_seconds)
        return max(delay, self._min_interval)

    async def run_forever(self) -> None:
        logger.info("ingestion scheduler started (interval=%ss)", self._interval)
        while not self._stopping.is_set():
            result = await self.run_cycle()
            delay = self.next_delay(result)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
            except TimeoutError:
                continue
        logger.info("ingestion scheduler stopped")

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self.run_forever(), name="ingestion-scheduler")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            await self._task
            self._task = None
