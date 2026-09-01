"""One-shot FusionSolar connectivity check.

Reads configuration from backend/.env (run it FROM the backend/ directory)
and performs one login + one station list + one KPI fetch, then exits.

In real mode this consumes ~3 of the ~5 calls / 10 min Northbound budget:
do NOT run it while the ingestion scheduler is running (single session —
a second login would invalidate the scheduler's token), and wait ~10
minutes before starting the scheduler afterwards.

Usage:
    python scripts/check_fusionsolar.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.base import AdapterAuthError, AdapterError, AdapterRateLimitError  # noqa: E402
from app.adapters.fusionsolar import build_fusionsolar_adapter  # noqa: E402
from app.config import get_settings  # noqa: E402


async def main() -> int:
    settings = get_settings()
    print(f"FUSIONSOLAR_MODE = {settings.fusionsolar_mode}")
    if settings.fusionsolar_mode == "real":
        print(f"FUSIONSOLAR_BASE_URL = {settings.fusionsolar_base_url}")
        print("NOTE: this check consumes ~3 API calls of the ~5/10min budget.\n")

    try:
        adapter = build_fusionsolar_adapter(settings)
    except ValueError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        await adapter.authenticate()
        print("[1/3] login ................ OK")

        plants = await adapter.list_plants()
        print(f"[2/3] station list ......... OK ({len(plants)} plants)")
        for plant in plants[:10]:
            capacity = f"{plant.capacity_kwp:.0f} kWp" if plant.capacity_kwp else "capacity n/a"
            print(f"      - {plant.vendor_plant_id}  |  {plant.name}  |  {capacity}")
        if len(plants) > 10:
            print(f"      ... and {len(plants) - 10} more")
        if not plants:
            print("      WARNING: 0 plants — check the API account's plant scope.")
            return 1

        sample = [p.vendor_plant_id for p in plants[:5]]
        readings = await adapter.fetch_plant_kpis(sample)
        print(f"[3/3] realtime KPIs ........ OK ({len(readings)} readings)")
        for r in readings:
            print(
                f"      - {r.vendor_plant_id}: status={r.status.value}, "
                f"power={r.active_power_kw} kW, today={r.daily_energy_kwh} kWh, "
                f"PR={r.performance_ratio}"
            )
        print("\nSUCCESS — credentials and connectivity are good.")
        print("Wait ~10 minutes before starting the scheduler (rate budget).")
        return 0

    except AdapterAuthError as exc:
        print(f"\nAUTH FAILED: {exc}", file=sys.stderr)
        print(
            "Check FUSIONSOLAR_USERNAME / FUSIONSOLAR_PASSWORD (the Northbound "
            "'systemCode', not your portal login) and the account's API scope.",
            file=sys.stderr,
        )
        return 3
    except AdapterRateLimitError as exc:
        print(f"\nRATE LIMITED: {exc}", file=sys.stderr)
        print("Wait 10-15 minutes and try again. Do not retry immediately.", file=sys.stderr)
        return 4
    except AdapterError as exc:
        print(f"\nVENDOR/NETWORK ERROR: {exc}", file=sys.stderr)
        print(
            "Check FUSIONSOLAR_BASE_URL — it must be your portal's host plus "
            "/thirdData, e.g. https://intl.fusionsolar.huawei.com/thirdData",
            file=sys.stderr,
        )
        return 5
    finally:
        await adapter.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
