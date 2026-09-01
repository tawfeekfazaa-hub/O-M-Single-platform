"""Huawei FusionSolar Northbound adapter (see docs/API-NOTES.md)."""

from __future__ import annotations

from app.adapters.fusionsolar.adapter import FusionSolarAdapter
from app.adapters.fusionsolar.client import RealFusionSolarClient
from app.adapters.fusionsolar.mock_client import MockFusionSolarClient
from app.config import Settings
from app.core.ratelimit import RollingWindowRateLimiter

__all__ = ["FusionSolarAdapter", "build_fusionsolar_adapter"]


def build_fusionsolar_adapter(settings: Settings) -> FusionSolarAdapter:
    """Build the adapter in the mode selected by configuration.

    Mock mode needs no credentials and is the default everywhere; real mode
    requires base URL + credentials and applies the client-side rate limit.
    """
    if settings.fusionsolar_mode == "mock":
        return FusionSolarAdapter(MockFusionSolarClient())

    missing = [
        name
        for name, value in (
            ("FUSIONSOLAR_BASE_URL", settings.fusionsolar_base_url),
            ("FUSIONSOLAR_USERNAME", settings.fusionsolar_username),
            ("FUSIONSOLAR_PASSWORD", settings.fusionsolar_password),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"FUSIONSOLAR_MODE=real requires env vars: {', '.join(missing)}")

    limiter = RollingWindowRateLimiter(
        max_calls=settings.fusionsolar_max_calls_per_window,
        window_seconds=settings.fusionsolar_window_seconds,
    )
    client = RealFusionSolarClient(
        base_url=settings.fusionsolar_base_url,  # type: ignore[arg-type]
        username=settings.fusionsolar_username,  # type: ignore[arg-type]
        password=settings.fusionsolar_password,  # type: ignore[arg-type]
        rate_limiter=limiter,
    )
    return FusionSolarAdapter(client)
