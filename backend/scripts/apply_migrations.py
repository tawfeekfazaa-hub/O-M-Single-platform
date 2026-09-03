"""Apply (or roll back) the SQL migrations in backend/migrations/.

    DATABASE_URL=postgresql+asyncpg://... python scripts/apply_migrations.py
    DATABASE_URL=... python scripts/apply_migrations.py --status
    DATABASE_URL=... python scripts/apply_migrations.py --down-to 001_initial_schema.sql
    DATABASE_URL=... python scripts/apply_migrations.py --down-to base

The runner itself lives in app/db/migrations.py so it can be exercised by the
integration tests; this file is the operator entry point. Rollback and recovery
procedures are documented in docs/MIGRATIONS.md.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from contextlib import suppress
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db.migrations import (  # noqa: E402
    MigrationError,
    apply_pending,
    downgrade_to,
    status,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def say(message: str, *, err: bool = False) -> None:
    """Print without letting a broken stream change the run's outcome.

    The runner already refuses to let its own reporting reverse a result. These
    are the CLI's own lines, and they were outside that: with stdout closed, a
    successful run reached `print("applied N migration(s)")`, raised
    BrokenPipeError into the handler below, and exited 2 — telling automation to
    handle a refusal for migrations that had been applied.
    """
    with suppress(Exception):
        print(message, file=sys.stderr if err else sys.stdout)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--status", action="store_true", help="list migrations and whether each is applied"
    )
    group.add_argument(
        "--down-to",
        metavar="FILENAME|base",
        help="roll back every migration applied AFTER this one ('base' unwinds all)",
    )
    parser.add_argument(
        "--adopt-legacy-checksums",
        action="store_true",
        help=(
            "record checksums for migrations applied by the pre-checksum runner. "
            "Only after confirming the database matches the current files: this "
            "records an UNVERIFIED baseline."
        ),
    )
    return parser.parse_args()


async def main() -> int:
    args = _parse_args()
    engine = None
    try:
        # Settings and engine are both built inside the handler. get_settings()
        # validates the WHOLE application config, so an unrelated invalid value
        # — a stale variable, a bad FUSIONSOLAR_MODE — raises a Pydantic error
        # before a migration run that only needs DATABASE_URL has begun; and a
        # malformed URL or an unavailable dialect makes create_async_engine()
        # raise synchronously. Either reaches the operator as a traceback unless
        # it is caught here.
        settings = get_settings()
        if not settings.database_url:
            say("DATABASE_URL is not set — nothing to migrate.", err=True)
            return 1

        engine = create_async_engine(settings.database_url)
        if args.status:
            drifted = 0
            for state in await status(engine, MIGRATIONS_DIR):
                mark = "applied" if state.applied else "pending"
                note = "" if state.has_down else "   (no .down.sql)"
                if state.drift:
                    drifted += 1
                    note = f"   !! {state.drift}"
                say(f"{mark:8} {state.filename}{note}")
            if drifted:
                say(f"\n{drifted} migration(s) drifted from what was applied.", err=True)
                return 2
        elif args.down_to is not None:  # empty string must reach validation, not apply
            count = await downgrade_to(
                engine,
                MIGRATIONS_DIR,
                args.down_to,
                adopt_legacy=args.adopt_legacy_checksums,
            )
            say(f"rolled back {count} migration(s)")
        else:
            count = await apply_pending(
                engine, MIGRATIONS_DIR, adopt_legacy=args.adopt_legacy_checksums
            )
            say(f"applied {count} migration(s)")
    except MigrationError as exc:
        # Names and reasons only — never file contents, never connection details.
        say(f"migration refused: {exc}", err=True)
        return 2
    except Exception as exc:
        # The documented contract is that a failed run exits 2 with a reason.
        # Anything the runner did not itself recognise would otherwise reach the
        # operator as a traceback — measured with the server simply not running
        # (ConnectionRefusedError) and with a pooled connection killed by a
        # restart (an asyncpg InternalClientError). The exception TYPE only: a
        # connection error's message carries the host and port.
        say(f"migration refused: the run failed ({type(exc).__name__})", err=True)
        return 2
    finally:
        if engine is not None:
            await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
