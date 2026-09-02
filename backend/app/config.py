"""Application settings. All values come from environment variables / .env.

Never hardcode credentials anywhere in the codebase — see CLAUDE.md rule 1.
Credential values (system code / deprecated password) must never be logged
or printed; only variable NAMES may appear in messages.
"""

import math
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

THIRD_DATA_PATH = "/thirdData"

# Budgets, windows and cadences all end up in the same float arithmetic: the
# rolling-window limiters, the scheduler's spacing maths and asyncio.sleep().
# They are validated HERE, on the settings object itself, so that every entry
# point is covered — `uvicorn app.main:app` builds the scheduler straight from
# Settings and never calls the diagnostic script's checks.
_FINITE_POSITIVE_SETTINGS = (
    "fusionsolar_login_max_calls",
    "fusionsolar_station_list_max_calls",
    "fusionsolar_station_list_max_pages",
)
# Every setting measured in SECONDS: a rolling window, a cadence or a sleep.
_POSITIVE_DURATION_SETTINGS = (
    "fusionsolar_login_window_seconds",
    "fusionsolar_station_list_window_seconds",
    "fusionsolar_kpi_window_seconds",
    "fusionsolar_inventory_refresh_seconds",
    "scheduler_interval_seconds",
)
# Zero is a deliberate choice here (no extra margin), not a misconfiguration.
_NON_NEGATIVE_DURATION_SETTINGS = ("fusionsolar_kpi_margin_seconds",)

# A duration is only a duration if it can elapse. One year is far beyond any
# real budget window (the widest default is a day) or cadence, so anything
# above it is a units mistake or an accidental exponent — and it fails the
# same way an infinity does: a window that never frees its slots stops the
# ingestion for good, a cadence that never comes due refreshes the inventory
# once and never again, and a sleep that long never wakes. Finite arithmetic
# hides all three, so they are rejected at the settings boundary instead.
_MAX_DURATION_SECONDS = 366 * 24 * 60 * 60.0


def _usable_number(value: float, *, allow_zero: bool, maximum: float | None = None) -> bool:
    """Finite, in range, and representable in the float math downstream.

    NaN and infinity pass every "<= 0" test but break their consumer for
    good: a NaN window never prunes its limiter history, an infinite one
    never frees a slot, a NaN cadence makes the elapsed-time comparison
    never true (the inventory is refreshed once and never again), and a
    non-finite poll interval is slept on and never wakes. A
    huge-but-parseable INTEGER is just as unusable, and converting it
    raises OverflowError rather than returning a value. ``maximum`` covers
    the finite values that are just as unreachable in practice.
    """
    try:
        as_float = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    if not math.isfinite(as_float):
        return False
    if maximum is not None and as_float > maximum:
        return False
    return as_float >= 0 if allow_zero else as_float > 0


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
    # urlsplit strips the brackets from an IPv6 literal; without them the
    # rebuilt authority would be ambiguous (and rejected by the client).
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    netloc = host if parts.port is None else f"{host}:{parts.port}"
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

    # --- removed in PR-1, kept only to fail the upgrade loudly ---
    # A single global budget could not model Huawei's per-endpoint limits.
    # extra="ignore" would drop these names silently, and the replacements
    # default LOOSER than a tightened global cap, so an operator upgrading
    # with them set would quietly lose the protection they configured.
    fusionsolar_max_calls_per_window: int | None = None
    fusionsolar_window_seconds: float | None = None

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

    # Per-FIELD validators on purpose: the reported loc is the field name,
    # which IS the environment variable name, so every caller's error
    # handler can name the offending variable without touching its value.
    @field_validator("fusionsolar_max_calls_per_window")
    @classmethod
    def _reject_removed_global_budget(cls, value: int | None) -> int | None:
        if value is not None:
            raise ValueError(
                "was replaced by per-endpoint budgets: set FUSIONSOLAR_LOGIN_MAX_CALLS "
                "and FUSIONSOLAR_STATION_LIST_MAX_CALLS instead, then remove it"
            )
        return value

    @field_validator("fusionsolar_window_seconds")
    @classmethod
    def _reject_removed_global_window(cls, value: float | None) -> float | None:
        if value is not None:
            raise ValueError(
                "was replaced by per-endpoint budgets: set FUSIONSOLAR_LOGIN_WINDOW_SECONDS, "
                "FUSIONSOLAR_STATION_LIST_WINDOW_SECONDS and FUSIONSOLAR_KPI_WINDOW_SECONDS "
                "instead, then remove it"
            )
        return value

    @field_validator(*_FINITE_POSITIVE_SETTINGS)
    @classmethod
    def _require_finite_positive(cls, value: float) -> float:
        if not _usable_number(value, allow_zero=False):
            raise ValueError("must be a finite value > 0")
        return value

    @field_validator(*_POSITIVE_DURATION_SETTINGS)
    @classmethod
    def _require_usable_positive_duration(cls, value: float) -> float:
        if not _usable_number(value, allow_zero=False, maximum=_MAX_DURATION_SECONDS):
            raise ValueError(
                f"must be a finite number of seconds > 0 and <= {_MAX_DURATION_SECONDS:.0f} "
                "(one year); a longer duration never elapses in practice"
            )
        return value

    @field_validator(*_NON_NEGATIVE_DURATION_SETTINGS)
    @classmethod
    def _require_usable_non_negative_duration(cls, value: float) -> float:
        if not _usable_number(value, allow_zero=True, maximum=_MAX_DURATION_SECONDS):
            raise ValueError(
                f"must be a finite number of seconds >= 0 and <= {_MAX_DURATION_SECONDS:.0f} "
                "(one year); a longer duration never elapses in practice"
            )
        return value

    @property
    def effective_system_code(self) -> str | None:
        """The systemCode to use: canonical variable, else deprecated alias."""
        return self.fusionsolar_system_code or self.fusionsolar_password


@lru_cache
def get_settings() -> Settings:
    return Settings()
