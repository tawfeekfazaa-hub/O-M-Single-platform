"""FastAPI application factory.

Wiring per configuration (all via env, see .env.example):
- repository: Postgres/TimescaleDB when DATABASE_URL is set, else in-memory
- adapter: FusionSolar mock (default) or real
- scheduler: runs inside this process only when SCHEDULER_ENABLED=true;
  it is the sole owner of vendor API calls
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.adapters.fusionsolar import build_fusionsolar_adapter
from app.api.routes import health, plants
from app.config import Settings, get_settings
from app.repositories.base import Repository
from app.repositories.memory import InMemoryRepository
from app.scheduler.ingestion import IngestionScheduler

logger = logging.getLogger(__name__)


def _build_repository(settings: Settings) -> Repository:
    if settings.database_url:
        # Imported lazily so mock-mode deployments don't need DB drivers loaded.
        from app.repositories.postgres import PostgresRepository

        return PostgresRepository.from_url(settings.database_url)
    logger.warning("DATABASE_URL not set — using in-memory repository (data is not persisted)")
    return InMemoryRepository()


class RealIngestionBlockedError(RuntimeError):
    """Raised when real scheduled ingestion is requested before it is allowed."""


def enforce_pre_quarantine_gate(settings: Settings) -> None:
    """Refuse real scheduled ingestion until Raw/Quarantine storage exists.

    PR-2 will add Raw/Quarantine tables; until then no live Huawei data may
    be ingested on a schedule (README security rules). Mock mode and a real
    configuration WITHOUT the scheduler remain allowed.
    """
    if settings.scheduler_enabled and settings.fusionsolar_mode == "real":
        raise RealIngestionBlockedError(
            "Real scheduled ingestion is blocked until Raw/Quarantine storage "
            "(PR-2) is implemented. Set FUSIONSOLAR_MODE=mock or "
            "SCHEDULER_ENABLED=false."
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    enforce_pre_quarantine_gate(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Defense in depth: the gate also guards direct lifespan execution.
        enforce_pre_quarantine_gate(settings)
        repository = _build_repository(settings)
        app.state.settings = settings
        app.state.repository = repository
        scheduler: IngestionScheduler | None = None
        adapter = None
        if settings.scheduler_enabled:
            adapter = build_fusionsolar_adapter(settings)
            min_interval = 0.0
            if settings.fusionsolar_mode == "real":  # pragma: no cover - gated
                min_interval = (
                    settings.fusionsolar_kpi_window_seconds
                    + settings.fusionsolar_kpi_margin_seconds
                )
            scheduler = IngestionScheduler(
                adapter,
                repository,
                interval_seconds=settings.scheduler_interval_seconds,
                min_interval_seconds=min_interval,
                inventory_refresh_seconds=settings.fusionsolar_inventory_refresh_seconds,
            )
            app.state.scheduler = scheduler
            scheduler.start()
        try:
            yield
        finally:
            if scheduler is not None:
                await scheduler.stop()
            if adapter is not None:
                await adapter.close()
            await repository.close()

    app = FastAPI(title="AQ O&M Platform API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    app.include_router(health.router, prefix="/api/v1")
    app.include_router(plants.router, prefix="/api/v1")
    return app


app = create_app()
