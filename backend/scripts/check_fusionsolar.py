"""Safe FusionSolar diagnostic utility (offline by default).

PROHIBITION NOTICE — a live check remains forbidden until ALL of:
PR-2 Raw/Quarantine storage is merged, an approved staging host exists,
and the company data-location policy decision is made (README, docs/
FUSIONSOLAR-CONTRACT.md). This script is prepared for that future moment;
running it live today is a policy violation even though the code exists.

Modes
-----
default            offline configuration validation + dry-run call plan.
--live             FUTURE live connectivity check. Requires ALL of:
                   --live, --i-understand-rate-budget, FUSIONSOLAR_MODE=real,
                   complete real-mode configuration, SCHEDULER_ENABLED=false.
                   Shows the planned maximum calls, caps them, prints only
                   counts/status — never station IDs, names, addresses,
                   payloads, KPI values, tokens, cookies, or credentials.

Exit codes (deterministic)
--------------------------
0 success · 2 configuration error (including unparseable settings) ·
3 authentication failed ·
4 rate-limited · 5 vendor/transport error · 6 safety refusal ·
7 protocol/contract violation

This script is NEVER run by CI and consumes rate budget only in the
(currently prohibited) --live mode.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.base import (  # noqa: E402
    AdapterAuthError,
    AdapterError,
    AdapterProtocolError,
    AdapterRateLimitError,
    AdapterTransientError,
)
from app.adapters.fusionsolar import (  # noqa: E402
    FusionSolarAdapter,
    build_fusionsolar_adapter,
)
from app.config import Settings, get_settings  # noqa: E402

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_AUTH = 3
EXIT_RATE = 4
EXIT_VENDOR = 5
EXIT_SAFETY = 6
EXIT_PROTOCOL = 7

# Hard cap on vendor calls a single live run may consume.
LIVE_MAX_CALLS = 4  # 1 login + up to 2 station-list pages + 1 KPI batch
# Pagination is capped BEFORE any call is made, so login + pages + the
# reserved KPI batch can never exceed LIVE_MAX_CALLS regardless of tenant
# size or the configured page guard. Inventories with more pages are
# validated by the scheduler, not by this checker.
LIVE_MAX_STATION_LIST_PAGES = LIVE_MAX_CALLS - 2  # minus login, minus KPI

assert 1 + LIVE_MAX_STATION_LIST_PAGES + 1 <= LIVE_MAX_CALLS


def live_capped_settings(settings: Settings) -> Settings:
    """Settings for a live run: page guard clamped under the call cap."""
    return settings.model_copy(
        update={
            "fusionsolar_station_list_max_pages": min(
                settings.fusionsolar_station_list_max_pages, LIVE_MAX_STATION_LIST_PAGES
            )
        }
    )


def _plan_lines(settings: Settings) -> list[str]:
    return [
        f"planned maximum vendor calls for one live run (hard cap {LIVE_MAX_CALLS}):",
        "  - POST /login .............. 1 call  (login budget)",
        f"  - POST /getStationList ..... up to {LIVE_MAX_STATION_LIST_PAGES} pages, "
        "hard-capped up front (station-list budget)",
        "  - POST /getStationRealKpi .. 1 batch, first <=100 plants (KPI budget)",
        "the cap is enforced at the transport level and covers every request,",
        "including a failCode 305 re-login and its retry.",
        f"profile={settings.fusionsolar_api_profile} mode={settings.fusionsolar_mode}",
    ]


def validate_config(settings: Settings) -> list[str]:
    """Names-only validation report. Never returns a value of any secret.

    Budgets, windows and cadences are NOT checked here: Settings validates
    them itself, so `uvicorn app.main:app` — which never runs this script —
    is covered by the same rule. What is left is mode-dependent and cannot
    live on the field: real mode needs a base URL and credentials, mock
    mode needs neither.
    """
    problems: list[str] = []
    if settings.fusionsolar_mode == "real":
        if not settings.fusionsolar_base_url:
            problems.append("FUSIONSOLAR_BASE_URL is not set")
        if not settings.fusionsolar_username:
            problems.append("FUSIONSOLAR_USERNAME is not set")
        if not settings.effective_system_code:
            problems.append("FUSIONSOLAR_SYSTEM_CODE is not set")
    return problems


def sanitize_error(exc: Exception) -> str:
    """Map an adapter error to a category string safe for terminals/logs."""
    if isinstance(exc, AdapterAuthError):
        return "auth-failed (check FUSIONSOLAR_USERNAME / FUSIONSOLAR_SYSTEM_CODE names)"
    if isinstance(exc, AdapterRateLimitError):
        return "rate-limited (wait for the endpoint window before retrying)"
    if isinstance(exc, AdapterProtocolError):
        return "protocol-violation (unexpected vendor response shape)"
    if isinstance(exc, AdapterTransientError):
        return "transient-network-failure (timeout/connection/5xx)"
    if isinstance(exc, AdapterError):
        return "vendor-error"
    return "unexpected-error"


async def run_live(settings: Settings, adapter: FusionSolarAdapter | None = None) -> int:
    """FUTURE live path — see the prohibition notice above.

    ``adapter`` is injectable for offline tests only; the default builds
    the real adapter from page-capped settings.
    """
    if adapter is None:
        try:
            adapter = build_fusionsolar_adapter(
                live_capped_settings(settings),
                # Absolute transport-level ceiling: not even a post-305
                # re-login and its retry may exceed the advertised cap.
                max_total_calls=LIVE_MAX_CALLS,
            )
        except ValueError as exc:
            # Unusable configuration (e.g. a non-positive budget) -> the
            # documented deterministic exit code, never a traceback.
            print(f"CONFIG ERROR: {exc}", file=sys.stderr)
            return EXIT_CONFIG
    calls_used = 0
    try:
        await adapter.authenticate()
        calls_used += 1
        print("[1/3] login ................ OK")

        plants = await adapter.list_plants()
        inv = adapter.last_inventory_diagnostics
        calls_used += inv.calls_consumed
        if calls_used >= LIVE_MAX_CALLS:
            # The last budgeted slot is RESERVED for the KPI batch; landing
            # on the cap here means pagination consumed it — stop.
            print("call cap reached — stopping before KPI fetch")
            return EXIT_OK
        inventory_counts = (
            f"(count={inv.stations}, pages={inv.pages_retrieved}, variant={inv.variant}, "
            f"invalid_capacity={inv.invalid_capacity})"
        )
        if inv.invalid_capacity:
            # The scheduler counts these same rows as an incomplete cycle
            # (their stored capacity is deliberately kept rather than
            # overwritten); a contract check must not certify them as sane.
            print(f"[2/3] station list ......... INCOMPLETE {inventory_counts}")
            print(
                "\nFAILED: the station list carried unreadable capacity values "
                "(counts above); the contract is NOT validated.",
                file=sys.stderr,
            )
            return EXIT_PROTOCOL
        print(f"[2/3] station list ......... OK {inventory_counts}")
        if not plants:
            print("      WARNING: 0 plants — check the API account's plant scope.")
            return EXIT_OK

        sample = [p.vendor_plant_id for p in plants[:100]]
        readings = await adapter.fetch_plant_kpis(sample)
        kpi = adapter.last_kpi_diagnostics
        counts = (
            f"(requested={kpi.requested}, returned={len(readings)}, "
            f"missing={kpi.missing}, duplicate={kpi.duplicates}, "
            f"unexpected={kpi.unexpected}, invalid={kpi.invalid_values})"
        )
        if not kpi.complete:
            # The scheduler treats these same diagnostics as an incomplete
            # ingestion; a contract check must not certify them as sane.
            print(f"[3/3] realtime KPIs ........ INCOMPLETE {counts}")
            print(
                "\nFAILED: the vendor response was incomplete or malformed "
                "(counts above); the contract is NOT validated.",
                file=sys.stderr,
            )
            return EXIT_PROTOCOL
        print(f"[3/3] realtime KPIs ........ OK {counts}")
        print("\nSUCCESS — connectivity and contract look sane (counts only).")
        return EXIT_OK
    except AdapterAuthError as exc:
        print(f"FAILED: {sanitize_error(exc)}", file=sys.stderr)
        return EXIT_AUTH
    except AdapterRateLimitError as exc:
        print(f"FAILED: {sanitize_error(exc)}", file=sys.stderr)
        return EXIT_RATE
    except AdapterProtocolError as exc:
        print(f"FAILED: {sanitize_error(exc)}", file=sys.stderr)
        return EXIT_PROTOCOL
    except AdapterError as exc:
        print(f"FAILED: {sanitize_error(exc)}", file=sys.stderr)
        return EXIT_VENDOR
    finally:
        # The HTTP client is closed on every path.
        await adapter.close()


def main(argv: list[str] | None = None, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_fusionsolar",
        description="Offline FusionSolar configuration check / dry-run planner.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="perform real vendor calls (PROHIBITED until PR-2 + hosting + policy)",
    )
    parser.add_argument(
        "--i-understand-rate-budget",
        action="store_true",
        dest="ack_budget",
        help="required with --live: acknowledge the vendor rate budget cost",
    )
    args = parser.parse_args(argv)
    if settings is None:
        try:
            settings = get_settings()
        except ValidationError as exc:
            # Unparseable configuration (e.g. an http:// base URL or a
            # non-numeric budget) must still produce the documented exit
            # code. Report the offending variable NAMES only — never the
            # values, never a traceback.
            # A model-level validator (e.g. the SYSTEM_CODE/PASSWORD
            # conflict) reports an EMPTY loc tuple — indexing it blindly
            # would raise IndexError inside this very handler.
            names = sorted({str(error["loc"][0]).upper() for error in exc.errors() if error["loc"]})
            print(
                "CONFIG ERROR: settings could not be loaded; check "
                + (", ".join(names) if names else "the FUSIONSOLAR_* variables"),
                file=sys.stderr,
            )
            return EXIT_CONFIG

    print(f"FUSIONSOLAR_MODE = {settings.fusionsolar_mode}")
    print(f"FUSIONSOLAR_API_PROFILE = {settings.fusionsolar_api_profile}")

    problems = validate_config(settings)
    for line in _plan_lines(settings):
        print(line)
    if problems:
        for problem in problems:
            print(f"CONFIG ERROR: {problem}", file=sys.stderr)
        return EXIT_CONFIG

    if not args.live:
        print("\nDRY RUN ONLY — no vendor call was made. Use --live (prohibited")
        print("until PR-2 + approved hosting + data-location policy) to connect.")
        return EXIT_OK

    # ---- live-mode safety interlocks (all must hold) ----
    if not args.ack_budget:
        print(
            "SAFETY REFUSAL: --live requires --i-understand-rate-budget",
            file=sys.stderr,
        )
        return EXIT_SAFETY
    if settings.fusionsolar_mode != "real":
        print("SAFETY REFUSAL: --live requires FUSIONSOLAR_MODE=real", file=sys.stderr)
        return EXIT_SAFETY
    if settings.scheduler_enabled:
        print(
            "SAFETY REFUSAL: the scheduler must be disabled during a live check "
            "(single vendor session)",
            file=sys.stderr,
        )
        return EXIT_SAFETY

    return asyncio.run(run_live(settings))


if __name__ == "__main__":
    raise SystemExit(main())
