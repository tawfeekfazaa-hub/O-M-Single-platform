"""Application settings. All values come from environment variables / .env.

Never hardcode credentials anywhere in the codebase — see CLAUDE.md rule 1.
Credential values (system code / deprecated password) must never be logged
or printed; only variable NAMES may appear in messages.
"""

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

THIRD_DATA_PATH = "/thirdData"


def normalize_fusionsolar_base_url(raw: str) -> str:
    """Validate and normalize the FusionSolar Northbound base URL.

    Rules: HTTPS only, no embedded credentials, no query or fragment, and
    the path is normalized to exactly ``/thirdData`` (accepting a bare host,
    a trailing slash, or an explicit /thirdData with optional trailing /).
    """
    parts = urlsplit(raw.strip())
    if parts.scheme != "https":
        raise ValueError("FUSIONSOLAR_BASE_URL must use https://")
    if not parts.hostname:
        raise ValueError("FUSIONSOLAR_BASE_URL must include a host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("FUSIONSOLAR_BASE_URL must not embed credentials")
    if parts.query or parts.fragment:
        raise ValueError("FUSIONSOLAR_BASE_URL must not contain a query or fragment")
    path = parts.path.rstrip("/")
    if path not in ("", THIRD_DATA_PATH):
        raise ValueError(f"FUSIONSOLAR_BASE_URL path must be empty or {THIRD_DATA_PATH}")
    netloc = parts.hostname if parts.port is None else f"{parts.hostname}:{parts.port}"
    return f"https://{netloc}{THIRD_DATA_PATH}"


class Settings(BaseSettings):
    # hide_input_in_errors keeps credential values out of ValidationError
    # messages (they would otherwise echo the offending input).
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    # Database. Unset -> in-memory repository (dev/CI/mock mode).
    database_url: str | None = None

    # FusionSolar adapter. Mock mode is the default everywhere; "real" is
    # for the scheduler process only, and real *scheduled* ingestion stays
    # blocked until Raw/Quarantine storage exists (PR-2 — see app.main).
    fusionsolar_mode: Literal["mock", "real"] = "mock"

    # API contract profile. PR-1 implements exactly one profile: the legacy
    # Northbound account (userName + systemCode + XSRF-TOKEN) speaking
    # /thirdData/getStationList. The OAuth //thirdData/stations stack is a
    # documented future upgrade path (docs/FUSIONSOLAR-CONTRACT.md) and is
    # deliberately NOT accepted here.
    fusionsolar_api_profile: Literal["legacy_system_code"] = "legacy_system_code"

    fusionsolar_base_url: str | None = None
    fusionsolar_username: str | None = None
    # Canonical secret for the Northbound account. Huawei calls this value
    # "systemCode"; it is a dedicated API credential, not a portal password.
    fusionsolar_system_code: str | None = None
    # DEPRECATED alias kept for backward compatibility with pre-PR-1 .env
    # files. Use FUSIONSOLAR_SYSTEM_CODE instead.
    fusionsolar_password: str | None = None

    # --- FusionSolar rate budgets (client-side, per endpoint) ---
    # login: officially documented as 5 calls / 10 min per user; default
    # keeps a margin of one call. (confirmed limit, safety-margin default)
    fusionsolar_login_max_calls: int = 4
    fusionsolar_login_window_seconds: float = 600.0
    # station list: Huawei documents a small daily-style budget whose exact
    # formula varies by SmartPVMS version; 4/day is our SAFETY DEFAULT, not
    # an official constant (docs/FUSIONSOLAR-CONTRACT.md).
    fusionsolar_station_list_max_calls: int = 4
    fusionsolar_station_list_window_seconds: float = 86_400.0
    # real-time KPI: officially ceil(plants/100) calls per 5 minutes, max
    # 100 station codes per call. The call count is derived at runtime from
    # the requested plant count; only the window is configured here.
    fusionsolar_kpi_window_seconds: float = 300.0
    # Extra margin the real-mode scheduler adds on top of the KPI window.
    # (safety default)
    fusionsolar_kpi_margin_seconds: float = 30.0

    # Station-list pagination guard (finite upper bound, safety default).
    fusionsolar_station_list_max_pages: int = 50

    # --- Ingestion scheduler ---
    scheduler_enabled: bool = False
    scheduler_interval_seconds: float = 300.0
    # Station inventory is refreshed on its own conservative cadence and
    # must NOT be fetched on every KPI cycle. (safety default: 6 hours)
    # This is a LOWER bound: a paginated inventory spends one station-list
    # call per page, so the scheduler stretches the effective spacing to
    # pages x window / budget (6 h holds only for a one-page inventory on
    # the 4/day default; e.g. 2 pages -> 12 h between refreshes).
    fusionsolar_inventory_refresh_seconds: float = 21_600.0

    cors_origins: str = "http://localhost:3000"

    @field_validator("fusionsolar_base_url")
    @classmethod
    def _validate_base_url(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        return normalize_fusionsolar_base_url(value)

    @model_validator(mode="after")
    def _resolve_system_code(self) -> "Settings":
        code, legacy = self.fusionsolar_system_code, self.fusionsolar_password
        if code and legacy and code != legacy:
            # Never echo either value — names only.
            raise ValueError(
                "FUSIONSOLAR_SYSTEM_CODE and deprecated FUSIONSOLAR_PASSWORD are both "
                "set with different values; remove FUSIONSOLAR_PASSWORD"
            )
        return self

    @property
    def effective_system_code(self) -> str | None:
        """The systemCode to use: canonical variable, else deprecated alias."""
        return self.fusionsolar_system_code or self.fusionsolar_password


@lru_cache
def get_settings() -> Settings:
    return Settings()
