"""Application settings. All values come from environment variables / .env.

Never hardcode credentials anywhere in the codebase — see CLAUDE.md rule 1.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database. Unset -> in-memory repository (dev/CI/mock mode).
    database_url: str | None = None

    # FusionSolar adapter. Mock mode is the default everywhere; "real" is
    # only for the scheduler process in staging/prod (CLAUDE.md rule 3).
    fusionsolar_mode: Literal["mock", "real"] = "mock"
    fusionsolar_base_url: str | None = None
    fusionsolar_username: str | None = None
    fusionsolar_password: str | None = None

    # FusionSolar Northbound rate limit: ~5 calls per 10 minutes per user,
    # failCode 407 on excess (docs/API-NOTES.md). Keep a safety margin.
    fusionsolar_max_calls_per_window: int = 4
    fusionsolar_window_seconds: float = 600.0

    # Ingestion scheduler.
    scheduler_enabled: bool = False
    scheduler_interval_seconds: float = 300.0

    cors_origins: str = "http://localhost:3000"


@lru_cache
def get_settings() -> Settings:
    return Settings()
