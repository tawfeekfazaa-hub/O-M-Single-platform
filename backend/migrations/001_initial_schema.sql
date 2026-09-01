-- 001: initial schema — plants, KPI hypertable, alarms stub.
-- Applied by backend/scripts/apply_migrations.py (docs/DECISIONS.md ADR-003).

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE plants (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vendor          TEXT NOT NULL,
    vendor_plant_id TEXT NOT NULL,
    name            TEXT NOT NULL,
    capacity_kwp    DOUBLE PRECISION,
    address         TEXT,
    status          TEXT NOT NULL DEFAULT 'unknown',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_plants_vendor_key UNIQUE (vendor, vendor_plant_id)
);

-- Time-series KPIs (IEC 61724-1 subset, Phase 1). Hypertable partitioned
-- on ts; every query must be scoped by plant_id (per-plant isolation).
CREATE TABLE kpi_measurements (
    ts                 TIMESTAMPTZ NOT NULL,
    plant_id           BIGINT NOT NULL REFERENCES plants(id) ON DELETE CASCADE,
    active_power_kw    DOUBLE PRECISION,
    daily_energy_kwh   DOUBLE PRECISION,
    total_energy_kwh   DOUBLE PRECISION,
    performance_ratio  DOUBLE PRECISION,
    PRIMARY KEY (plant_id, ts)
);

SELECT create_hypertable('kpi_measurements', 'ts', chunk_time_interval => INTERVAL '7 days');

CREATE INDEX idx_kpi_plant_ts_desc ON kpi_measurements (plant_id, ts DESC);

-- Alarms stub (ingestion pipeline is a later phase; schema reserved now).
CREATE TABLE alarms (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    plant_id        BIGINT NOT NULL REFERENCES plants(id) ON DELETE CASCADE,
    vendor_alarm_id TEXT,
    severity        TEXT NOT NULL,
    message         TEXT NOT NULL,
    raised_at       TIMESTAMPTZ NOT NULL,
    cleared_at      TIMESTAMPTZ
);

CREATE INDEX idx_alarms_plant_raised ON alarms (plant_id, raised_at DESC);
