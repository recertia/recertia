-- id: 003_idempotency
-- REL-5 F20: durable idempotency keys. Same key + same hash replays.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    tenant_id     TEXT NOT NULL,
    scope         TEXT NOT NULL,
    key           TEXT NOT NULL,
    request_hash  TEXT NOT NULL,
    response_json TEXT,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, scope, key)
);
