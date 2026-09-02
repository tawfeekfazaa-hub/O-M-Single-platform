"""Safe-checker tests: offline default, safety interlocks, redaction,
deterministic exit codes. No network is ever touched here."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from app.adapters.base import (
    AdapterAuthError,
    AdapterError,
    AdapterProtocolError,
    AdapterRateLimitError,
    AdapterTransientError,
)
from app.config import Settings

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_fusionsolar.py"
spec = importlib.util.spec_from_file_location("check_fusionsolar", SCRIPT)
check = importlib.util.module_from_spec(spec)
sys.modules["check_fusionsolar"] = check
spec.loader.exec_module(spec and check)  # type: ignore[union-attr]

SECRET = "super-secret-system-code"


def make_settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def test_default_is_dry_run_with_zero_vendor_calls(capsys: pytest.CaptureFixture):
    code = check.main([], settings=make_settings())
    out = capsys.readouterr().out
    assert code == check.EXIT_OK
    assert "DRY RUN ONLY" in out
    assert "no vendor call was made" in out


def test_dry_run_shows_planned_call_budget(capsys: pytest.CaptureFixture):
    check.main([], settings=make_settings())
    out = capsys.readouterr().out
    assert "planned maximum vendor calls" in out
    assert "/login" in out and "/getStationList" in out and "/getStationRealKpi" in out


def test_real_mode_missing_config_is_config_error(capsys: pytest.CaptureFixture):
    code = check.main([], settings=make_settings(fusionsolar_mode="real"))
    err = capsys.readouterr().err
    assert code == check.EXIT_CONFIG
    assert "FUSIONSOLAR_BASE_URL" in err  # names only


def test_live_without_budget_ack_is_refused(capsys: pytest.CaptureFixture):
    settings = make_settings(
        fusionsolar_mode="real",
        fusionsolar_base_url="https://host.test/thirdData",
        fusionsolar_username="user",
        fusionsolar_system_code=SECRET,
    )
    code = check.main(["--live"], settings=settings)
    assert code == check.EXIT_SAFETY
    captured = capsys.readouterr()
    assert "--i-understand-rate-budget" in captured.err
    assert SECRET not in captured.out + captured.err


def test_live_in_mock_mode_is_refused():
    code = check.main(["--live", "--i-understand-rate-budget"], settings=make_settings())
    assert code == check.EXIT_SAFETY


def test_live_with_scheduler_enabled_is_refused(capsys: pytest.CaptureFixture):
    # Scheduler on: must refuse before any client is built.
    settings = make_settings(
        fusionsolar_mode="real",
        scheduler_enabled=True,
        fusionsolar_base_url="https://host.test/thirdData",
        fusionsolar_username="user",
        fusionsolar_system_code=SECRET,
    )
    code = check.main(["--live", "--i-understand-rate-budget"], settings=settings)
    assert code == check.EXIT_SAFETY
    assert "scheduler" in capsys.readouterr().err.lower()


def test_no_output_ever_contains_the_system_code(capsys: pytest.CaptureFixture):
    settings = make_settings(
        fusionsolar_mode="real",
        fusionsolar_base_url="https://host.test/thirdData",
        fusionsolar_username="user",
        fusionsolar_system_code=SECRET,
    )
    check.main([], settings=settings)
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (AdapterAuthError("boom"), "auth-failed"),
        (AdapterRateLimitError("boom"), "rate-limited"),
        (AdapterProtocolError("boom"), "protocol-violation"),
        (AdapterTransientError("boom"), "transient-network-failure"),
        (AdapterError("boom"), "vendor-error"),
        (ValueError("boom"), "unexpected-error"),
    ],
)
def test_error_sanitizer_yields_categories_only(exc: Exception, expected: str):
    message = check.sanitize_error(exc)
    assert expected in message
    assert "boom" not in message  # raw exception text never leaks


def test_exit_codes_are_deterministic_constants():
    assert (
        check.EXIT_OK,
        check.EXIT_CONFIG,
        check.EXIT_AUTH,
        check.EXIT_RATE,
        check.EXIT_SAFETY,
        check.EXIT_PROTOCOL,
    ) == (0, 2, 3, 4, 6, 7)
