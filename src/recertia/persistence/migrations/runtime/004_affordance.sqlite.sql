-- id: 004_affordance
-- REC-056d: affordance counters per tenant. Increments are SQL in one BEGIN IMMEDIATE txn.
-- seq is a per-tool sequence; an error signature keeps the seq of the record that last saw it.
CREATE TABLE IF NOT EXISTS affordance_tools (
    tenant_id   TEXT NOT NULL,
    tool        TEXT NOT NULL,
    invocations INTEGER NOT NULL DEFAULT 0,
    failures    INTEGER NOT NULL DEFAULT 0,
    seq         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, tool)
);

CREATE TABLE IF NOT EXISTS affordance_tool_samples (
    id         INTEGER PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    tool       TEXT NOT NULL,
    duration_s REAL NOT NULL,
    cost_usd   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS affordance_tool_samples_key
    ON affordance_tool_samples (tenant_id, tool, id);

CREATE TABLE IF NOT EXISTS affordance_error_signatures (
    tenant_id TEXT NOT NULL,
    tool      TEXT NOT NULL,
    signature TEXT NOT NULL,
    count     INTEGER NOT NULL DEFAULT 0,
    last_seq  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, tool, signature)
);

CREATE TABLE IF NOT EXISTS affordance_resources (
    tenant_id        TEXT NOT NULL,
    kind             TEXT NOT NULL,
    rid              TEXT NOT NULL,
    conflicts        INTEGER NOT NULL DEFAULT 0,
    timeouts         INTEGER NOT NULL DEFAULT 0,
    observed_ceiling INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id, kind, rid)
);

CREATE TABLE IF NOT EXISTS affordance_resource_waits (
    id        INTEGER PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    kind      TEXT NOT NULL,
    rid       TEXT NOT NULL,
    wait_ms   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS affordance_resource_waits_key
    ON affordance_resource_waits (tenant_id, kind, rid, id);
