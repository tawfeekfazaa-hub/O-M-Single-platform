"""SQLAlchemy Core table metadata.

Must stay in sync with backend/migrations/*.sql — the SQL files are the
source of truth for DDL (docs/DECISIONS.md ADR-003); these definitions
exist for query building only and are never used to create tables.
"""

from __future__ import annotations

import sqlalchemy as sa

metadata = sa.MetaData()

plants = sa.Table(
    "plants",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("vendor", sa.Text, nullable=False),
    sa.Column("vendor_plant_id", sa.Text, nullable=False),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("capacity_kwp", sa.Double),
    sa.Column("address", sa.Text),
    sa.Column("status", sa.Text, nullable=False, server_default="unknown"),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.UniqueConstraint("vendor", "vendor_plant_id", name="uq_plants_vendor_key"),
)

kpi_measurements = sa.Table(
    "kpi_measurements",
    metadata,
    sa.Column("ts", sa.TIMESTAMP(timezone=True), primary_key=True),
    sa.Column("plant_id", sa.BigInteger, sa.ForeignKey("plants.id"), primary_key=True),
    sa.Column("active_power_kw", sa.Double),
    sa.Column("daily_energy_kwh", sa.Double),
    sa.Column("total_energy_kwh", sa.Double),
    sa.Column("performance_ratio", sa.Double),
)

alarms = sa.Table(
    "alarms",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("plant_id", sa.BigInteger, sa.ForeignKey("plants.id"), nullable=False),
    sa.Column("vendor_alarm_id", sa.Text),
    sa.Column("severity", sa.Text, nullable=False),
    sa.Column("message", sa.Text, nullable=False),
    sa.Column("raised_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("cleared_at", sa.TIMESTAMP(timezone=True)),
)
