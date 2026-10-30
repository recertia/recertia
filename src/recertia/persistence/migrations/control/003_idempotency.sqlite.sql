-- id: 003_idempotency
-- Idempotency-Key results for POST /v1/programs/{program_id}/steps/{step_id}/run (REC-056e).
-- A row is 'pending' while its request binds the step and 'complete' once the bind is saved.
-- Times are integer unix seconds (UTC). Expired rows are swept on write.
CREATE TABLE idempotency_keys (
    tenant_id       TEXT NOT NULL,
    program_id      TEXT NOT NULL,
    step_id         TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    state           TEXT NOT NULL CHECK (state IN ('pending', 'complete')),
    run_id          TEXT,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, program_id, step_id, idempotency_key),
    CHECK ((state = 'pending' AND run_id IS NULL) OR (state = 'complete' AND run_id IS NOT NULL))
);

CREATE INDEX idempotency_keys_expires_at ON idempotency_keys (expires_at);
