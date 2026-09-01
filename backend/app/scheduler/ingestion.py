"""Central ingestion scheduler.

The ONLY component allowed to call vendor adapters (CLAUDE.md rule 2).
One periodic cycle: list plants -> upsert -> fetch KPIs -> store. On
rate-limit or transient vendor errors it backs off exponentially with
jitter instead of hammering the API. Clock/sleep/jitter are injectable so
tests run instantly.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.adapters.base import AdapterError, AdapterRateLimitError, VendorAdapter
from app.repositories.base import Repository

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CycleResult:
    plants_upserted: int = 0
    readings_written: int = 0
    error: str | None = None
    rate_limited: bool = False
    retry_after_seconds: float | None = None


@dataclass(slots=True)
class SchedulerStats:
    cycles_total: int = 0
    cycles_failed: int = 0
    consecutive_failures: int = 0
    last_result: CycleResult | None = None


class IngestionScheduler:
    def __init__(
        self,
        adapter: VendorAdapter,
        repository: Repository,
        *,
        interval_seconds: float = 300.0,
        backoff_base_seconds: float = 60.0,
        backoff_max_seconds: float = 1800.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._adapter = adapter
        self._repository = repository
        self._interval = interval_seconds
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds
        self._sleep = sleep
        self._jitter = jitter
        self.stats = SchedulerStats()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def run_cycle(self) -> CycleResult:
        """One ingestion pass. Never raises — errors land in the result."""
        result = CycleResult()
        try:
            await self._adapter.authenticate()
            plants = await self._adapter.list_plants()
            await self._repository.upsert_plants(plants)
            result.plants_upserted = len(plants)
            readings = await self._adapter.fetch_plant_kpis([p.vendor_plant_id for p in plants])
            result.readings_written = await self._repository.record_kpis(readings)
        except AdapterRateLimitError as exc:
            result.rate_limited = True
            result.error = str(exc)
            result.retry_after_seconds = exc.retry_after_seconds
            logger.warning("ingestion rate-limited: %s", exc)
        except AdapterError as exc:
            result.error = str(exc)
            logger.error("ingestion cycle failed: %s", exc)

        self.stats.cycles_total += 1
        if result.error is None:
            self.stats.consecutive_failures = 0
        else:
            self.stats.cycles_failed += 1
            self.stats.consecutive_failures += 1
        self.stats.last_result = result
        return result

    def next_delay(self, result: CycleResult) -> float:
        """Normal interval on success; exponential backoff + jitter on failure."""
        if result.error is None:
            return self._interval
        exponent = min(self.stats.consecutive_failures - 1, 5)
        delay = min(self._backoff_max, self._backoff_base * (2**exponent))
        # The vendor's retry-after hint is a lower bound on the wait.
        if result.retry_after_seconds is not None:
            delay = max(delay, result.retry_after_seconds)
        # 0.75x..1.25x jitter so multiple deployments don't sync up.
        return delay * (0.75 + 0.5 * self._jitter())

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
