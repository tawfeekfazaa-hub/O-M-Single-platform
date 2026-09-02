"""Configuration contract tests: credentials naming, URL validation, profile."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings, normalize_fusionsolar_base_url


def make_settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def test_system_code_is_canonical():
    settings = make_settings(fusionsolar_system_code="canonical-value")
    assert settings.effective_system_code == "canonical-value"


def test_deprecated_password_still_works_as_alias():
    settings = make_settings(fusionsolar_password="legacy-value")
    assert settings.effective_system_code == "legacy-value"


def test_matching_duplicate_values_are_tolerated():
    settings = make_settings(fusionsolar_system_code="same", fusionsolar_password="same")
    assert settings.effective_system_code == "same"


def test_conflicting_credential_values_fail_without_echoing_them():
    with pytest.raises(ValidationError) as excinfo:
        make_settings(fusionsolar_system_code="value-one", fusionsolar_password="value-two")
    message = str(excinfo.value)
    assert "FUSIONSOLAR_SYSTEM_CODE" in message
    assert "value-one" not in message and "value-two" not in message


def test_api_profile_only_accepts_legacy_system_code():
    assert make_settings().fusionsolar_api_profile == "legacy_system_code"
    with pytest.raises(ValidationError):
        make_settings(fusionsolar_api_profile="oauth")


@pytest.mark.parametrize(
    "raw",
    [
        "https://intl.example-portal.test",
        "https://intl.example-portal.test/",
        "https://intl.example-portal.test/thirdData",
        "https://intl.example-portal.test/thirdData/",
    ],
)
def test_base_url_is_normalized_to_third_data(raw: str):
    assert normalize_fusionsolar_base_url(raw) == "https://intl.example-portal.test/thirdData"


def test_base_url_preserves_explicit_port():
    normalized = normalize_fusionsolar_base_url("https://host.test:8443/thirdData")
    assert normalized == "https://host.test:8443/thirdData"


@pytest.mark.parametrize(
    "raw",
    [
        "http://intl.example-portal.test/thirdData",  # not https
        "https://user@host.test/thirdData",  # embedded credentials (userinfo)
        "https://host.test/thirdData?x=1",  # query
        "https://host.test/thirdData#frag",  # fragment
        "https://host.test/otherPath",  # foreign path
        "https://",  # no host
    ],
)
def test_base_url_rejects_unsafe_forms(raw: str):
    with pytest.raises(ValueError):
        normalize_fusionsolar_base_url(raw)


def test_settings_validator_applies_url_normalization():
    settings = make_settings(fusionsolar_base_url="https://host.test/")
    assert settings.fusionsolar_base_url == "https://host.test/thirdData"
    with pytest.raises(ValidationError):
        make_settings(fusionsolar_base_url="http://host.test/")


def test_rate_budget_safety_defaults():
    settings = make_settings()
    assert settings.fusionsolar_login_max_calls == 4
    assert settings.fusionsolar_login_window_seconds == 600.0
    assert settings.fusionsolar_station_list_window_seconds == 86_400.0
    assert settings.fusionsolar_kpi_window_seconds == 300.0
    assert settings.fusionsolar_inventory_refresh_seconds == 21_600.0
