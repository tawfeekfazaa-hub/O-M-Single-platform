from __future__ import annotations

from fastapi import APIRouter, Request

from app.schemas.plants import HealthOut

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
async def health(request: Request) -> HealthOut:
    scheduler = getattr(request.app.state, "scheduler", None)
    settings = request.app.state.settings
    return HealthOut(
        status="ok",
        fusionsolar_mode=settings.fusionsolar_mode,
        scheduler_enabled=scheduler is not None,
        scheduler_cycles_total=scheduler.stats.cycles_total if scheduler else 0,
        scheduler_cycles_failed=scheduler.stats.cycles_failed if scheduler else 0,
    )
