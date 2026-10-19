-- id: 003_hop
-- REL-5 hop commit: trajectory and generic outbox live in runtime.db.
CREATE TABLE IF NOT EXISTS trajectory_events (
    tenant_id  TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    hop_seq    INTEGER NOT NULL,
    event_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, run_id, seq)
);
CREATE TABLE IF NOT EXISTS trajectory_meta (
    tenant_id TEXT NOT NULL,
    run_id    TEXT NOT NULL,
    meta_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, run_id)
);
CREATE TABLE IF NOT EXISTS route_entries (
    tenant_id  TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    entry_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, run_id, seq)
);
CREATE TABLE IF NOT EXISTS outbox (
    id            INTEGER PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    kind          TEXT NOT NULL,
    dedupe_key    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('pending','delivered','dead')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    available_at  TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    delivered_at  TEXT,
    UNIQUE (tenant_id, kind, dedupe_key)
);
