-- id: 001_baseline
-- Checksummed starting point for a new runtime.db. Not applied to checkpoints.db.
CREATE TABLE IF NOT EXISTS checkpoints (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    node TEXT NOT NULL,
    next_node TEXT,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS operations (
    run_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    node TEXT NOT NULL,
    op_seq INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY (run_id, attempt_no, node, op_seq)
);
