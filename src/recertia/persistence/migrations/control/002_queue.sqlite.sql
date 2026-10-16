-- id: 002_queue
-- Durable run queue. In-flight is a query of claimed/running rows, not a counter.
CREATE TABLE run_queue (
    tenant_id     TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('run', 'eval', 'job')),
    status        TEXT NOT NULL CHECK (status IN (
                      'queued', 'claimed', 'running', 'succeeded',
                      'failed', 'cancelled', 'dead'
                  )),
    priority      INTEGER NOT NULL DEFAULT 0,
    enqueued_at   TEXT NOT NULL,
    not_before    TEXT NOT NULL,
    claimed_by    TEXT,
    claim_token   INTEGER NOT NULL DEFAULT 0,
    heartbeat_at  TEXT,
    attempt       INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    payload_json  TEXT NOT NULL,
    error_code    TEXT,
    finished_at   TEXT,
    PRIMARY KEY (tenant_id, run_id, kind)
);

CREATE INDEX run_queue_claimable
    ON run_queue (status, not_before, enqueued_at);

CREATE TABLE workers (
    worker_id    TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    hostname     TEXT NOT NULL,
    pid          INTEGER,
    capacity     INTEGER NOT NULL,
    inflight     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE tenant_sched (
    tenant_id     TEXT PRIMARY KEY,
    deficit       INTEGER NOT NULL DEFAULT 0,
    weight        INTEGER NOT NULL DEFAULT 1,
    last_claim_at TEXT
);

CREATE TABLE tenant_limits (
    tenant_id                   TEXT PRIMARY KEY,
    max_runs_per_day            INTEGER NOT NULL,
    max_cost_usd_per_day        REAL NOT NULL,
    max_in_flight               INTEGER NOT NULL,
    max_queue_depth             INTEGER NOT NULL,
    max_sse_connections         INTEGER NOT NULL,
    max_model_spend_usd_per_day REAL NOT NULL,
    weekly_token_cap            INTEGER
);

CREATE TABLE tenant_usage_daily (
    tenant_id TEXT NOT NULL,
    day       TEXT NOT NULL,
    runs      INTEGER NOT NULL DEFAULT 0,
    cost_usd  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, day)
);

CREATE TABLE maintenance (
    id     INTEGER PRIMARY KEY CHECK (id = 1),
    mode   TEXT NOT NULL CHECK (mode IN ('open', 'quiescing', 'frozen')),
    reason TEXT,
    since  TEXT NOT NULL
);

INSERT INTO maintenance (id, mode, since) VALUES (1, 'open', '1970-01-01T00:00:00Z');
