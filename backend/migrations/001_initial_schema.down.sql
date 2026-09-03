-- Rollback of 001_initial_schema.sql.
--
-- DESTRUCTIVE: dropping these tables discards every stored plant, KPI
-- measurement and alarm. Read docs/MIGRATIONS.md before running this against
-- anything but a test database.
--
-- The timescaledb EXTENSION is deliberately NOT dropped: other databases in the
-- cluster may depend on it, and re-creating it is cheap while losing it is not.

DROP TABLE IF EXISTS alarms;
DROP TABLE IF EXISTS kpi_measurements;   -- hypertable; DROP TABLE removes its chunks
DROP TABLE IF EXISTS plants;
