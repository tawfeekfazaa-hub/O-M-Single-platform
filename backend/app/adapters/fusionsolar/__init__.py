"""Huawei FusionSolar Northbound adapter (legacy_system_code profile).

See docs/FUSIONSOLAR-CONTRACT.md for the normative contract, budgets and
the (out-of-scope) OAuth //thirdData/stations upgrade path.
"""

from __future__ import annotations

from app.adapters.fusionsolar.adapter import FusionSolarAdapter
from app.adapters.fusionsolar.client import RealFusionSolarClient
from app.adapters.fusionsolar.mock_client import MockFusionSolarClient
from app.adapters.fusionsolar.policy import FusionSolarRatePolicy
from app.config import Settings

__all__ = ["FusionSolarAdapter", "build_fusionsolar_adapter"]


def build_fusionsolar_adapter(settings: Settings) -> FusionSolarAdapter:
    """Build the adapter in the mode selected by configuration.

    Mock mode needs no credentials and is the default everywhere; only the
    mock adapter is allowed to map the synthetic mock-only KPI fields.
    Real mode requires the base URL, user name and system code, and wires
    the per-endpoint rate policy. No other API profile is accepted.
    """
    if settings.fusionsolar_api_profile != "legacy_system_code":  # defense in depth
        raise ValueError("only the legacy_system_code FusionSolar profile is implemented")

    if settings.fusionsolar_mode == "mock":
        return FusionSolarAdapter(MockFusionSolarClient(), allow_synthetic_fields=True)

    missing = [
        name
        for name, value in (
            ("FUSIONSOLAR_BASE_URL", settings.fusionsolar_base_url),
            ("FUSIONSOLAR_USERNAME", settings.fusionsolar_username),
            ("FUSIONSOLAR_SYSTEM_CODE", settings.effective_system_code),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"FUSIONSOLAR_MODE=real requires env vars: {', '.join(missing)}")

    client = RealFusionSolarClient(
        base_url=settings.fusionsolar_base_url,  # type: ignore[arg-type]
        username=settings.fusionsolar_username,  # type: ignore[arg-type]
        system_code=settings.effective_system_code,  # type: ignore[arg-type]
        policy=FusionSolarRatePolicy.from_settings(settings),
        max_station_list_pages=settings.fusionsolar_station_list_max_pages,
    )
    return FusionSolarAdapter(client, allow_synthetic_fields=False)
