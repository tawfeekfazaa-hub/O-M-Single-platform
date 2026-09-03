"""Pre-quarantine safety gate: real scheduled ingestion must be rejected
until Raw/Quarantine storage (PR-2) exists. Mock stays fully functional."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.main import RealIngestionBlockedError, create_app, enforce_pre_quarantine_gate


def make_settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def test_real_plus_scheduler_is_rejected_at_app_creation():
    settings = make_settings(
        fusionsolar_mode="real",
        scheduler_enabled=True,
        fusionsolar_base_url="https://host.test/thirdData",
        fusionsolar_username="user",
        fusionsolar_system_code="not-a-real-code",
    )
    with pytest.raises(RealIngestionBlockedError) as excinfo:
        create_app(settings)
    assert "Raw/Quarantine" in str(excinfo.value)
    assert "not-a-real-code" not in str(excinfo.value)  # never echo credentials


def test_real_plus_scheduler_rejected_even_without_credentials():
    # The gate fires before any credential/URL validation.
    settings = make_settings(fusionsolar_mode="real", scheduler_enabled=True)
    with pytest.raises(RealIngestionBlockedError):
        enforce_pre_quarantine_gate(settings)


async def test_mock_scheduler_mode_still_works():
    settings = make_settings(fusionsolar_mode="mock", scheduler_enabled=True)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert app.state.scheduler is not None
        result = await app.state.scheduler.run_cycle()
        assert result.error is None


def test_real_mode_without_scheduler_is_allowed():
    # Serving the API from stored data with real config but no scheduled
    # ingestion involves zero vendor calls and stays permitted.
    settings = make_settings(fusionsolar_mode="real", scheduler_enabled=False)
    app = create_app(settings)
    assert app is not None
