"""Apply SQL migrations from backend/migrations/ in filename order.

Usage: DATABASE_URL=postgresql+asyncpg://... python scripts/apply_migrations.py
Tracks applied files in schema_migrations; each migration runs in its own
transaction and is applied at most once.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


async def main() -> int:
    settings = get_settings()
    if not settings.database_url:
        print("DATABASE_URL is not set — nothing to migrate.", file=sys.stderr)
        return 1

    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "  filename TEXT PRIMARY KEY,"
                    "  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                    ")"
                )
            )
            applied = {
                row.filename
                for row in await conn.execute(sa.text("SELECT filename FROM schema_migrations"))
            }

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                print(f"skip  {path.name}")
                continue
            async with engine.begin() as conn:
                # asyncpg prepares statements, which forbids multi-statement
                # strings — run migration files on the raw driver connection.
                raw = await conn.get_raw_connection()
                await raw.driver_connection.execute(path.read_text())
                await conn.execute(
                    sa.text("INSERT INTO schema_migrations (filename) VALUES (:f)"),
                    {"f": path.name},
                )
            print(f"apply {path.name}")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
