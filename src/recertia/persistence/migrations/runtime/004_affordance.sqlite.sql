-- id: 004_affordance
-- REL-5 F19: affordance counters live in runtime.db, not a JSON document.
CREATE TABLE IF NOT EXISTS affordance_tools (
    tenant_id              TEXT NOT NULL,
    tool                   TEXT NOT NULL,
    invocations            INTEGER NOT NULL DEFAULT 0,
    failures               INTEGER NOT NULL DEFAULT 0,
    durations_json         TEXT NOT NULL DEFAULT '[]',
    error_signatures_json  TEXT NOT NULL DEFAULT '{}',
    costs_json             TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (tenant_id, tool)
);
CREATE TABLE IF NOT EXISTS affordance_resources (
    tenant_id         TEXT NOT NULL,
    kind              TEXT NOT NULL,
    resource_id       TEXT NOT NULL,
    waits_json        TEXT NOT NULL DEFAULT '[]',
    conflicts         INTEGER NOT NULL DEFAULT 0,
    timeouts          INTEGER NOT NULL DEFAULT 0,
    observed_ceiling  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id, kind, resource_id)
);
