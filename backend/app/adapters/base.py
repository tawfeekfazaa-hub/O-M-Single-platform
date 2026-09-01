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
    """One normalized KPI sample for one plant (IEC 61724-1 subset)."""

    vendor: str
    vendor_plant_id: str
    ts: datetime  # timezone-aware UTC
    active_power_kw: float | None = None
    daily_energy_kwh: float | None = None
    total_energy_kwh: float | None = None
    performance_ratio: float | None = None  # 0..1
    status: PlantStatus = PlantStatus.UNKNOWN


class AdapterError(Exception):
    """Base class for all vendor adapter failures."""


class AdapterAuthError(AdapterError):
    """Authentication failed or session expired and re-login failed."""


class AdapterRateLimitError(AdapterError):
    """Vendor rejected the call for exceeding its rate limit.

    ``retry_after_seconds`` is a hint for the scheduler's backoff; it is a
    lower bound, not a guarantee.
    """

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


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
