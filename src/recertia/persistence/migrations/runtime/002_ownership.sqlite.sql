-- id: 002_ownership
-- REL-3 run_leases. Presence of a row is HELD; delete on release.
CREATE TABLE IF NOT EXISTS run_leases (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    incarnation INTEGER NOT NULL,
    heartbeat_at TEXT NOT NULL,
    lock_path TEXT,
    PRIMARY KEY (tenant_id, run_id)
);
