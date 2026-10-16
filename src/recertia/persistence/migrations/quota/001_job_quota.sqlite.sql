-- id: 001_job_quota
-- Weekly job-quota ledger. The JSON sidecar is an export of the spent columns.
CREATE TABLE job_quota_week (
    tenant_id                    TEXT NOT NULL,
    week_id                      TEXT NOT NULL,
    tokens_spent                 INTEGER NOT NULL DEFAULT 0,
    tokens_reserved              INTEGER NOT NULL DEFAULT 0,
    hex_tokens_spent             INTEGER NOT NULL DEFAULT 0,
    hex_tokens_reserved          INTEGER NOT NULL DEFAULT 0,
    computer_use_tokens_spent    INTEGER NOT NULL DEFAULT 0,
    computer_use_tokens_reserved INTEGER NOT NULL DEFAULT 0,
    hex_jobs_by_class_json       TEXT NOT NULL DEFAULT '{}',
    hex_jobs_reserved_json       TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, week_id)
);

CREATE TABLE job_quota_ledger (
    tenant_id           TEXT NOT NULL,
    reservation_id      TEXT NOT NULL,
    week_id             TEXT NOT NULL,
    job_name            TEXT NOT NULL,
    priority            TEXT NOT NULL,
    tokens_reserved     INTEGER NOT NULL,
    tokens_settled      INTEGER,
    computer_use_tokens INTEGER NOT NULL DEFAULT 0,
    hex_tokens          INTEGER NOT NULL DEFAULT 0,
    task_class          TEXT,
    status              TEXT NOT NULL CHECK (status IN ('reserved', 'settled', 'released')),
    created_at          TEXT NOT NULL,
    settled_at          TEXT,
    PRIMARY KEY (tenant_id, reservation_id)
);
