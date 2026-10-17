# ADR-0021: Multi-tenant service with pooled control plane and isolated execution

- **Status:** accepted
- **Date:** 2026-10-16
- **Decides:** D1. D2–D5 are recorded in this ADR so later specs do not re-litigate them.
- **Related:** [ADR-0001](0001-graph-with-loops.md), [ADR-0009](0009-contracts-as-code.md), [ADR-0018](0018-idle-state-offloading.md), [ADR-0020](0020-store-receipts.md), [threat-model deltas](../architecture/threat-model-deltas.md), [operations](../architecture/operations.md)
- **Plan:** [docs/plans/remediation/README.md](../plans/remediation/README.md)

## Context

Recertia persists as a single operator tree: SQLite files and JSON under a gitignored `.recertia/` directory, opened by the process that is serving the request. That is the shape [operations](../architecture/operations.md) documents, and it is the shape the test suite and the local executor assume.

The product surface has already moved past a single operator. API keys carry a `tenant_id`. Quotas are counted per tenant. The console speaks OIDC and has a tenant switcher. Run identifiers are stored as `(tenant_id, run_id)`. None of that is a deployment model. Two API replicas would fork the ledger, share one container runtime, and read each other's session state.

The owner decision is that Recertia is a multi-tenant service. The single-operator tree remains a supported mode for development and for an operator who runs one node on purpose.

## Decision

D1. Recertia is a multi-tenant service with two planes.

1. **Control plane, pooled.** Stateless API replicas. Postgres is the system of record: every tenant-owned row carries `tenant_id`, and row-level security is on. Blobs, transcripts, snapshots, and the other large objects live in object storage under a `tenants/<tenant_id>/` prefix. Any replica can serve any tenant. A replica stores nothing that another replica must read from its local disk.
2. **Execution plane, isolated per tenant.** An executor service runs commands and containers for one tenant. The API process never holds a container runtime socket in `multi_node`. There is no local execution backend in `multi_node`.
3. **Tenants stay first-class in `single_node`.** Development, CI, and a self-hosted single node keep the SQLite-and-files tree. They still authenticate a tenant, still apply quotas, and still refuse a cross-tenant read. That is defence in depth, so a bug in path joining is caught before it depends on Postgres RLS. `single_node` is the default for development.

`Settings.deployment_mode` is `single_node` or `multi_node`. Startup of every process role (API, worker, CLI, executor client) refuses a `multi_node` configuration that is unsafe. The refused set is in Consequences. `RECERTIA_API_ALLOW_LOCAL_EXEC` has no effect in `multi_node`.

REL-10 (pooled control plane) and REL-11 (tenant lifecycle and fairness) are in scope. This ADR is already accepted, so REL-10 is not waiting on a later acceptance flip.

## Related decisions

These are part of this ADR. Specs cite them by id.

### D2 — `runs` may run commands; `exec` is for a raw script; local is single-node only

The `runs` scope may submit caller-supplied commands. Those commands run on the container backend. In `single_node` they may also run on the local backend, and only behind the existing API break-glass (`RECERTIA_API_ALLOW_LOCAL_EXEC=1`). The `exec` scope (or `admin`) is required only for a raw `script` body.

`multi_node` refuses the local backend at startup with `execution_unavailable` (CLI exit 7). An explicit golden-gate backend of `local` is refused the same way. The golden gate defaults to `inherit`, which resolves to the configured non-local backend.

Compiling a goal into criteria does not promote those criteria to `exec`. `file_exists` and `file_contains` paths are still quoted and confined to the workdir. See the remediation plan, hotfix 0.2.

### D3 — durability default is `normal`

`Settings.durability` is `strict`, `normal`, or `fast`. The default is `normal`: atomic replaces fsync the file and its directory, the ledger fsyncs every append, SQLite runs `synchronous=NORMAL` in WAL, and Postgres runs `synchronous_commit=on`. `fast` is for benchmarks and selected tests. Startup refuses `fast` when `deployment_mode=multi_node`.

### D4 — `/v1/runs` is async by default after one release

The release that ships the durable queue keeps `RECERTIA_API_DEFAULT_ASYNC=0`. Callers who want the new shape use enqueue-and-wait: the API enqueues, holds the request, and returns the terminal record. The following release flips the default to `1`. A client that omits `mode` then receives `202` and polls. `mode=sync` remains available through that release for callers that need the old body.

### D5 — the CLI is the local operator principal

Every CLI command builds `Principal.local_operator()`. That principal carries `platform_admin` and the actor `cli:<os user>@<host>`. It is the actor on ledger appends, cancel requests, tenant administration, ledger repair, and backup. A CLI command that touches one tenant takes `--tenant`. The legacy `--runs-root` layout, where the runs root is the process root, does not.

## Alternatives considered

- **Silo per customer.** One database, one object bucket, and one API deployment per tenant. Isolation is the process boundary, which is easy to explain and expensive to operate: migrations, backups, and the console fan out with tenant count. The tenant switcher and the shared key store would be lying about a boundary the deployment did not have. A customer who needs a silo runs `single_node` (or a dedicated `multi_node` cell with one tenant). That is a deployment choice, not the service model.
- **Fully pooled, including execution.** API replicas run containers themselves and share a runtime socket. One stuck runtime, one breakout, or one tenant's image pull then stalls every tenant, and the API holds the socket this decision forbids. Rejected.
- **Hybrid, this ADR.** Pool the control plane, where stateless replicas and RLS match the existing tenant keying. Isolate execution, where a container is a tenant boundary the database cannot express. `single_node` keeps today's tree so development does not require Postgres, object storage, or an executor fleet.

## Consequences

**In scope now**

- REL-10 and REL-11 ship in the remediation train. REL-10 is not gated on a further ADR.
- In `multi_node`, SEC-1, SEC-10, SEC-4c, SEC-6, SEC-8, and SEC-13 are the strict form of those controls: an unbound tenant fails closed, dev-login and the unsigned session fallback are off, the API holds no container socket, a tenant scope cannot repair the ledger, `platform_admin` is not implied by tenant `admin`, and per-tenant secrets are envelope-encrypted. RLS and the object-storage prefix are REL-10. Per-tenant admission caps fail closed under REL-11; changing a cap is SEC-8.
- SEC-2 is D2. Container execution of `runs`-scope commands is the normal path. Local execution of those commands exists only in `single_node` with the break-glass. Raw `script` stays on `exec`.

**Startup refuses `multi_node` when any of these hold**

| Condition | Error |
| --- | --- |
| Execution backend is `local`, or the golden gate resolves to `local` | `execution_unavailable`, exit 7 |
| `RECERTIA_DB_BACKEND` is not `postgres`, or `RECERTIA_DATABASE_URL` is missing, or the URL uses the owner role | `ConfigError`, exit 2 |
| `RECERTIA_BLOB_STORE` is not `s3` | `ConfigError`, exit 2 |
| `RECERTIA_CONSOLE_SESSION_SECRET` is unset | `ConfigError`, exit 2 |
| `RECERTIA_DURABILITY=fast` | `ConfigError`, exit 2 |
| `RECERTIA_ALLOW_FAULTS=1` | `ConfigError`, exit 2 |
| `RECERTIA_TENANT_ENCRYPTION` is not `required` | `ConfigError`, exit 2 |

`single_node` keeps the current local backend behind the API break-glass. The random session-secret fallback stays available only in that mode.

**Trade-offs**

- Two stores to migrate (SQLite files to Postgres, loose blobs to object storage) and a process boundary between the API and the executor.
- RLS is defence in depth on top of application checks, not a replacement for them. `single_node` has no RLS and must still pass the same tenant tests.
- Enqueue-and-wait (D4) holds a request thread for one release. That is the compatible bridge, and it is removed as the default in the following release.

## What this does not decide

The receipt as the durability unit ([ADR-0020](0020-store-receipts.md)), idle-state residency ([ADR-0018](0018-idle-state-offloading.md)), and the cyclic graph ([ADR-0001](0001-graph-with-loops.md)) stand. This ADR does not pick a Postgres major version, an object-store vendor beyond an S3 API, or a KMS vendor. Envelope encryption is SEC-13. REL-11 crypto-shreds those keys. Dependency review is SEC-11 and SEC-12. Schema and repository shape are ARCH-7 and REL-10, specified in the remediation plan.
