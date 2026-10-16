-- id: 001_baseline
-- Empty control baseline. REL-4 and REL-10 add queue and tenant tables.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    checksum TEXT,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
