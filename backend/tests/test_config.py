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


def test_ipv6_host_keeps_its_brackets():
    # urlsplit strips the brackets; without them the authority is malformed
    # and the HTTP client rejects the URL before any request is sent.
    assert (
        normalize_fusionsolar_base_url("https://[2001:db8::1]:8443/thirdData")
        == "https://[2001:db8::1]:8443/thirdData"
    )
    assert (
        normalize_fusionsolar_base_url("https://[2001:db8::1]") == "https://[2001:db8::1]/thirdData"
    )
    # A plain host is unaffected.
    assert (
        normalize_fusionsolar_base_url("https://host.test:8443")
        == "https://host.test:8443/thirdData"
    )


def test_station_list_page_guard_is_bounded_by_the_daily_budget():
    from app.adapters.fusionsolar import effective_station_list_page_guard

    # A refresh cannot be resumed across windows, so attempting more pages
    # than the budget allows would make the inventory unretrievable.
    assert (
        effective_station_list_page_guard(
            Settings(_env_file=None, fusionsolar_station_list_max_pages=50)
        )
        == 4  # the 4/day safety default
    )
    # A generous budget lets the configured page guard apply unchanged.
    assert (
        effective_station_list_page_guard(
            Settings(
                _env_file=None,
                fusionsolar_station_list_max_pages=10,
                fusionsolar_station_list_max_calls=40,
            )
        )
        == 10
    )


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("fusionsolar_inventory_refresh_seconds", float("nan")),
        ("scheduler_interval_seconds", float("inf")),
        ("fusionsolar_kpi_margin_seconds", float("nan")),
    ],
)
def test_the_application_entry_point_refuses_an_unusable_timing_setting(
    field: str, bad: float, monkeypatch: pytest.MonkeyPatch
):
    # `uvicorn app.main:app` runs create_app() -> get_settings() -> Settings()
    # straight from the environment and never touches the diagnostic script's
    # checks. Before the rule moved onto the field, SCHEDULER_ENABLED=true
    # started happily with a NaN cadence and refreshed its inventory exactly
    # once, so the load-from-environment path is what has to refuse.
    from app.config import get_settings

    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    monkeypatch.setenv(field.upper(), repr(bad))
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError) as excinfo:
            get_settings()
    finally:
        get_settings.cache_clear()
    assert [error["loc"] for error in excinfo.value.errors()] == [(field,)]


@pytest.mark.parametrize(
    "field",
    [
        "fusionsolar_login_window_seconds",
        "fusionsolar_station_list_window_seconds",
        "fusionsolar_kpi_window_seconds",
        "fusionsolar_inventory_refresh_seconds",
        "scheduler_interval_seconds",
        "fusionsolar_kpi_margin_seconds",
    ],
)
def test_a_duration_that_cannot_elapse_is_refused(field: str, monkeypatch: pytest.MonkeyPatch):
    # 1e308 is finite, so every isfinite() guard waves it through — and it
    # then fails exactly the way infinity does: SCHEDULER_INTERVAL_SECONDS
    # schedules the next wake ~1e300 years out, the inventory cadence never
    # comes due, and a rolling window never frees the slot it is holding.
    # A duration is only a duration if it can elapse.
    from app.config import get_settings

    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    monkeypatch.setenv(field.upper(), "1e308")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError) as excinfo:
            get_settings()
    finally:
        get_settings.cache_clear()
    assert [error["loc"] for error in excinfo.value.errors()] == [(field,)]


def test_the_duration_ceiling_leaves_realistic_configuration_alone():
    # The ceiling has to sit far above anything an operator would really
    # set, or it becomes the misconfiguration. A month-long window, a
    # week-long inventory cadence and a zero margin all stay legal.
    settings = make_settings(
        fusionsolar_station_list_window_seconds=30 * 86_400.0,
        fusionsolar_inventory_refresh_seconds=7 * 86_400.0,
        fusionsolar_kpi_margin_seconds=0.0,
    )
    assert settings.fusionsolar_station_list_window_seconds == 30 * 86_400.0
    assert settings.fusionsolar_inventory_refresh_seconds == 7 * 86_400.0
    assert settings.fusionsolar_kpi_margin_seconds == 0.0


@pytest.mark.parametrize(
    ("legacy", "replacement"),
    [
        ("FUSIONSOLAR_MAX_CALLS_PER_WINDOW", "FUSIONSOLAR_LOGIN_MAX_CALLS"),
        ("FUSIONSOLAR_WINDOW_SECONDS", "FUSIONSOLAR_LOGIN_WINDOW_SECONDS"),
    ],
)
def test_removed_global_budget_variables_fail_the_upgrade(
    legacy: str, replacement: str, monkeypatch: pytest.MonkeyPatch
):
    # PR-1 replaced one global budget with per-endpoint budgets. extra="ignore"
    # would drop the old names silently, and the replacements default LOOSER
    # than a tightened global cap: an operator upgrading with a 1-call cap set
    # would silently get 4 login and 4 station-list calls. The upgrade must
    # fail loudly instead, naming the variable and its replacement.
    from app.config import get_settings

    monkeypatch.setenv(legacy, "1")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError) as excinfo:
            get_settings()
    finally:
        get_settings.cache_clear()
    (error,) = excinfo.value.errors()
    assert error["loc"] == (legacy.lower(),)  # the loc IS the variable name
    assert replacement in error["msg"]
    assert "1" not in error["msg"].replace("PR-1", "")  # names only, never values


def test_the_removed_names_are_only_rejected_when_actually_set():
    # The compatibility guard must not reject a clean configuration.
    settings = make_settings()
    assert settings.fusionsolar_max_calls_per_window is None
    assert settings.fusionsolar_window_seconds is None
