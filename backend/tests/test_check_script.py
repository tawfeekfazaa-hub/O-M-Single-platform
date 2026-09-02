"""Safe-checker tests: offline default, safety interlocks, redaction,
deterministic exit codes. No network is ever touched here."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.adapters.base import (
    AdapterAuthError,
    AdapterError,
    AdapterProtocolError,
    AdapterRateLimitError,
    AdapterTransientError,
)
from app.adapters.fusionsolar.adapter import InventoryDiagnostics, KpiDiagnostics
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


def test_live_page_guard_is_capped_under_the_call_budget():
    capped = check.live_capped_settings(make_settings(fusionsolar_station_list_max_pages=50))
    assert capped.fusionsolar_station_list_max_pages == check.LIVE_MAX_STATION_LIST_PAGES
    # An already-lower guard is never raised.
    low = check.live_capped_settings(make_settings(fusionsolar_station_list_max_pages=1))
    assert low.fusionsolar_station_list_max_pages == 1
    # Arithmetic: login + capped pages + the reserved KPI batch fit the cap.
    assert 1 + check.LIVE_MAX_STATION_LIST_PAGES + 1 <= check.LIVE_MAX_CALLS


async def test_live_run_reserves_the_kpi_slot(capsys: pytest.CaptureFixture):
    # If pagination lands exactly on the cap, the KPI batch must NOT run —
    # the last budgeted slot is reserved for it, never added on top.
    class CapAdapter:
        def __init__(self) -> None:
            self.kpi_called = False
            self.closed = False
            self.last_inventory_diagnostics = InventoryDiagnostics(
                stations=250, pages_retrieved=3, variant="paginated", calls_consumed=3
            )
            self.last_kpi_diagnostics = KpiDiagnostics()

        async def authenticate(self) -> None:
            return None

        async def list_plants(self) -> list:
            return []

        async def fetch_plant_kpis(self, vendor_plant_ids: list[str]) -> list:
            self.kpi_called = True
            return []

        async def close(self) -> None:
            self.closed = True

    adapter = CapAdapter()
    code = await check.run_live(make_settings(), adapter=adapter)
    assert code == check.EXIT_OK
    assert adapter.kpi_called is False
    assert adapter.closed is True  # client closed on every path
    assert "stopping before KPI fetch" in capsys.readouterr().out


def test_exit_codes_are_deterministic_constants():
    assert (
        check.EXIT_OK,
        check.EXIT_CONFIG,
        check.EXIT_AUTH,
        check.EXIT_RATE,
        check.EXIT_SAFETY,
        check.EXIT_PROTOCOL,
    ) == (0, 2, 3, 4, 6, 7)


@pytest.mark.parametrize(
    ("field", "name"),
    [
        ("fusionsolar_station_list_max_calls", "FUSIONSOLAR_STATION_LIST_MAX_CALLS"),
        ("fusionsolar_login_max_calls", "FUSIONSOLAR_LOGIN_MAX_CALLS"),
        ("fusionsolar_kpi_window_seconds", "FUSIONSOLAR_KPI_WINDOW_SECONDS"),
        ("fusionsolar_station_list_max_pages", "FUSIONSOLAR_STATION_LIST_MAX_PAGES"),
    ],
)
def test_non_positive_safety_settings_are_config_errors(
    field: str, name: str, capsys: pytest.CaptureFixture
):
    # These would make the rate limiters unconstructible; the dry run must
    # report them rather than letting the live path die with a traceback.
    code = check.main([], settings=make_settings(**{field: 0}))
    assert code == check.EXIT_CONFIG
    assert name in capsys.readouterr().err  # names only, never values


async def test_unusable_budget_gives_a_config_exit_not_a_traceback(
    capsys: pytest.CaptureFixture,
):
    settings = make_settings(
        fusionsolar_mode="real",
        fusionsolar_base_url="https://host.test/thirdData",
        fusionsolar_username="user",
        fusionsolar_system_code=SECRET,
        fusionsolar_station_list_max_calls=0,
    )
    code = await check.run_live(settings)
    captured = capsys.readouterr()
    assert code == check.EXIT_CONFIG
    assert SECRET not in captured.out + captured.err


def test_dry_run_states_the_cap_covers_authentication_recovery(
    capsys: pytest.CaptureFixture,
):
    check.main([], settings=make_settings())
    out = capsys.readouterr().out
    assert "transport level" in out and "305" in out


def test_absurdly_large_integer_budget_is_a_config_error(capsys: pytest.CaptureFixture):
    # A huge-but-parseable integer passes pydantic, but float() overflows on
    # it — and that conversion happens past main()'s ValidationError handler,
    # so it used to print a traceback instead of the documented exit code.
    # The value is unusable anyway: the limiter and the scheduler's spacing
    # arithmetic are float maths.
    code = check.main([], settings=make_settings(fusionsolar_login_max_calls=int("1" * 1000)))
    captured = capsys.readouterr()
    assert code == check.EXIT_CONFIG
    assert "FUSIONSOLAR_LOGIN_MAX_CALLS" in captured.err  # names only
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_windows_are_config_errors(bad: float, capsys: pytest.CaptureFixture):
    # NaN/inf pass every "<= 0" test but break the limiter permanently: a
    # NaN window never prunes its history, an infinite one never frees a slot.
    code = check.main([], settings=make_settings(fusionsolar_kpi_window_seconds=bad))
    assert code == check.EXIT_CONFIG
    assert "FUSIONSOLAR_KPI_WINDOW_SECONDS" in capsys.readouterr().err


def test_unparseable_settings_exit_config_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    # get_settings() raising ValidationError (e.g. an http:// base URL) must
    # still produce the documented exit code, with variable NAMES only.
    monkeypatch.setenv("FUSIONSOLAR_BASE_URL", "http://insecure.test")
    from app.config import Settings as RealSettings

    monkeypatch.setattr(check, "get_settings", lambda: RealSettings(_env_file=None))
    code = check.main([])
    captured = capsys.readouterr()
    assert code == check.EXIT_CONFIG
    assert "CONFIG ERROR" in captured.err
    assert "FUSIONSOLAR_BASE_URL" in captured.err
    assert "insecure.test" not in captured.err  # the value is never echoed


@pytest.mark.parametrize(
    "diagnostics",
    [
        KpiDiagnostics(requested=2, returned=1, missing=1),
        KpiDiagnostics(requested=2, returned=2, duplicates=1),
        KpiDiagnostics(requested=2, returned=2, unexpected=1),
        KpiDiagnostics(requested=2, returned=2, invalid_values=1),
    ],
)
async def test_live_check_fails_on_incomplete_kpi_diagnostics(
    diagnostics: KpiDiagnostics, capsys: pytest.CaptureFixture
):
    # The scheduler treats these diagnostics as an incomplete ingestion, so a
    # contract check must not print SUCCESS and exit 0 for the same data.
    class Adapter:
        def __init__(self) -> None:
            self.closed = False
            self.last_inventory_diagnostics = InventoryDiagnostics(
                stations=2, pages_retrieved=1, variant="direct_list", calls_consumed=1
            )
            self.last_kpi_diagnostics = diagnostics

        async def authenticate(self) -> None:
            return None

        async def list_plants(self) -> list:
            return [
                SimpleNamespace(vendor_plant_id="NE=1"),
                SimpleNamespace(vendor_plant_id="NE=2"),
            ]

        async def fetch_plant_kpis(self, vendor_plant_ids: list[str]) -> list:
            return [object()] * diagnostics.returned

        async def close(self) -> None:
            self.closed = True

    adapter = Adapter()
    code = await check.run_live(make_settings(), adapter=adapter)
    captured = capsys.readouterr()
    assert code == check.EXIT_PROTOCOL
    assert "INCOMPLETE" in captured.out
    assert "NOT validated" in captured.err
    assert "SUCCESS" not in captured.out
    assert adapter.closed is True


async def test_live_check_reports_success_only_on_complete_diagnostics(
    capsys: pytest.CaptureFixture,
):
    class Adapter:
        def __init__(self) -> None:
            self.closed = False
            self.last_inventory_diagnostics = InventoryDiagnostics(
                stations=1, pages_retrieved=1, variant="direct_list", calls_consumed=1
            )
            self.last_kpi_diagnostics = KpiDiagnostics(requested=1, returned=1)

        async def authenticate(self) -> None:
            return None

        async def list_plants(self) -> list:
            return [SimpleNamespace(vendor_plant_id="NE=1")]

        async def fetch_plant_kpis(self, vendor_plant_ids: list[str]) -> list:
            return [object()]

        async def close(self) -> None:
            self.closed = True

    code = await check.run_live(make_settings(), adapter=Adapter())
    out = capsys.readouterr().out
    assert code == check.EXIT_OK
    assert "SUCCESS" in out and "duplicate=0" in out
