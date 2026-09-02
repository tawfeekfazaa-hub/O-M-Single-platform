"""Vendor adapter contract.

Every vendor integration (FusionSolar, Sungrow, ...) implements
``VendorAdapter`` and nothing outside ``app/adapters`` may talk to a vendor
API. Only the ingestion scheduler is allowed to call adapter methods that
hit the network (CLAUDE.md rule 2); the HTTP API layer must depend on the
repository, never on an adapter.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar


class PlantStatus(enum.StrEnum):
    HEALTHY = "healthy"
    FAULTY = "faulty"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PlantInfo:
    """Normalized plant metadata, vendor-agnostic."""

    vendor: str
    vendor_plant_id: str
    name: str
    capacity_kwp: float | None = None
    address: str | None = None


@dataclass(frozen=True, slots=True)
class PlantKpiReading:
    """One normalized KPI sample for one plant (IEC 61724-1 subset).

    Timestamp provenance:
    - ``ts`` is the local RECEIVED-AT time (when our process ingested the
      sample), timezone-aware UTC. It is what gets stored.
    - ``vendor_server_time`` is the vendor's SERVER clock as reported in
      the response envelope (FusionSolar ``params.currentTime``). It is
      NOT a device measurement timestamp and must not be presented as one.
    """

    vendor: str
    vendor_plant_id: str
    ts: datetime  # timezone-aware UTC, local received-at time
    active_power_kw: float | None = None
    daily_energy_kwh: float | None = None
    total_energy_kwh: float | None = None
    performance_ratio: float | None = None  # normalized 0..1
    status: PlantStatus = PlantStatus.UNKNOWN
    vendor_server_time: datetime | None = None  # vendor server clock, UTC


class AdapterError(Exception):
    """Base class for all vendor adapter failures."""


class AdapterAuthError(AdapterError):
    """Authentication failed or session expired and re-login failed."""


class AdapterTransientError(AdapterError):
    """Timeout, connection failure, or retryable 5xx — safe to retry LATER
    (after scheduler backoff), never immediately."""


class AdapterProtocolError(AdapterError):
    """The vendor answered with a malformed or contract-violating payload
    (non-JSON, unexpected envelope shape, impossible pagination metadata)."""


class AdapterRateLimitError(AdapterError):
    """Vendor rejected the call for exceeding its rate limit.

    ``retry_after_seconds`` is a hint for the scheduler's backoff; it is a
    lower bound, not a guarantee.

    ``retry_after_covers_whole_attempt`` says what that delay actually
    buys. A plain rate-limit hint frees ONE slot, which is not enough for a
    multi-call operation: retrying then would spend the freed slot, fail
    again at the same point and never make progress, so the scheduler
    widens such a delay to a full window. When the adapter has instead
    measured when the ENTIRE next attempt can run — a pre-flight capacity
    check that knows how many calls it needs — the delay is already
    sufficient and widening it only adds staleness.

    ``blocks_authentication`` marks a throttle on the endpoint that
    establishes the session. Every other call needs that session, so no
    further work is possible until the delay has passed: a caller that
    treats it as a throttle of the operation it happened to be running
    would move on to the next one, re-authenticate, and send another
    request to the very endpoint the vendor just throttled.
    """

    def __init__(
        self,
        message: str,
        retry_after_seconds: float | None = None,
        *,
        retry_after_covers_whole_attempt: bool = False,
        blocks_authentication: bool = False,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.retry_after_covers_whole_attempt = retry_after_covers_whole_attempt
        self.blocks_authentication = blocks_authentication


class VendorAdapter(ABC):
    """Contract every vendor integration must implement.

    Implementations must be safe to call sequentially from a single
    scheduler task; they are not required to be concurrency-safe.
    """

    #: Stable lowercase vendor key, e.g. "fusionsolar", "sungrow".
    vendor: ClassVar[str]

    @abstractmethod
    async def authenticate(self) -> None:
        """Establish (or refresh) a vendor session. Idempotent."""

    @abstractmethod
    async def list_plants(self) -> list[PlantInfo]:
        """Return all plants visible to the configured account."""

    @abstractmethod
    async def fetch_plant_kpis(self, vendor_plant_ids: list[str]) -> list[PlantKpiReading]:
        """Return the latest KPI reading for each requested plant.

        Implementations should batch: one vendor call for many plants
        wherever the vendor API allows it.
        """

    @abstractmethod
    async def health_check(self) -> bool:
        """Cheap liveness probe. Must NOT consume vendor rate budget in real mode."""

    async def close(self) -> None:
        """Release underlying resources (HTTP clients, sessions)."""
        return None
