# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Operator notes

Tenant-keyed control plane on the existing SQLite-and-files tree. Development
default is still `RECERTIA_DEPLOYMENT_MODE=single_node`. Production solves still
default to the container executor. The local break-glass child is
`/bin/sh -c` with `shell=False` (no `shell=True`). `multi_node` refuses SQLite, the local
backend, `RECERTIA_ALLOW_FAULTS=1`, `RECERTIA_TENANT_ENCRYPTION` other than
`required`, a database URL whose user is `recertia_owner` or `postgres`,
`RECERTIA_CONSOLE_AUTH=off` or `dev`, and `RECERTIA_CONSOLE_DEV_LOGIN`. It does
not yet run Postgres RLS, object storage, or a remote executor.

**Tenancy.** Runs, events, and blobs live under `root/runs/<tenant>/` and
`root/blobs/<tenant>/`. Memberships are `tenant_admin` or `operator`. The CLI is
the local operator (`platform_admin`, actor `cli:<user>@<host>`). Tenant-scoped
commands take `--tenant`. `recertia tenant export --tenant <id>` lists paths
(dry-run default). Copy with `--execute --dest <dir>` (`--dest` must sit under
the runs root or the system temp directory). No crypto-shred.

**CLI.** `--local-exec` on `run` / `resume` is a per-invocation override; it does
not write `RECERTIA_EXECUTION_BACKEND`. `recertia worker [--root .recertia]
[--once] [--concurrency 1]` drains the SQLite run queue (`control.db`). Sync
`POST /v1/runs` does not wait on that process. `recertia backup` / `backup create`
writes a `.tar.gz` with `MANIFEST.json` (sha256 per file). `recertia backup verify
<archive>` recomputes checksums. Backup and restore take the root lock and refuse
if the API holds it. `recertia ledger verify [--check-sig] [--check-anchor]`:
`--check-sig` verifies HMAC signatures (unsigned entries are allowed until the
first signed one); `--check-anchor` is reserved and skipped.
`recertia ledger rekey --dry-run` counts unsigned vs signed lines and does not
rewrite the ledger. The core CLI (`recertia --help`) does not require FastAPI or
the `[api]` extra.

**HTTP API.** Default `POST /v1/runs` is still sync.
`RECERTIA_API_DEFAULT_ASYNC` stays false. Sync create and resume enqueue and
wait up to 60s for the worker thread; a timeout returns `202`. `mode=async`
enqueues and returns `202 {run_id, status: queued}` unless a wait is requested.
Optional wait (async only): `?wait=<seconds>`, `X-Recertia-Wait`, or
`Prefer: wait` / `Prefer: wait=<seconds>` (cap 30s; timeout still returns 202).
`GET /metrics` returns Prometheus text and requires the `metrics` scope
(`admin` is accepted). `GET /v1/runs/{id}/events` resumes from `Last-Event-ID`
or `?offset=<bytes>`. `?follow=<seconds>` (cap 30) keeps the stream open for
new lines; omit it and the response is the backlog. `GET /v1/runs` is
tenant-scoped keyset pagination
(`limit` 1–100, default 50,
`cursor`; response `{items, next_cursor}`) from `run_summaries`. A second run
whose resolved workdir is already held returns `409` `workspace_busy`.
`RECERTIA_ENV=prod` turns off `/docs`, `/redoc`, and `/openapi.json`. The API
process sets umask `077`. A workspace snapshot is refused when free disk is
below 1.2 times the tree size (`disk_space_low`). `recertia gc` keeps
snapshots whose run id is still non-terminal.

**Auth and console.** API keys are HMAC-SHA256 with a pepper. Existing PBKDF2
rows stay valid and rehash on the next successful authenticate. `multi_node`
requires `RECERTIA_API_KEY_PEPPER`. `RECERTIA_CONSOLE_AUTH=off` (default)
ignores session cookies and `X-Recertia-Session` (API keys only). Session cookie
`recertia_session` is v2, `HttpOnly`, `SameSite=Strict`, and `Secure` except in
`dev`. CSRF is double-submit (`recertia_csrf` plus `X-Recertia-CSRF`). Cookie
POSTs with an `Origin` must match `Host`. OIDC sends a nonce and rejects a
reused `jti`. ID-token signature verification against JWKS is not in this
release.

**Quota, secrets, durability.** Weekly job spend is reserved then settled in
`job_quota.sqlite` beside the JSON sidecar. `{runs_root}/jobs/job_quota.json`
remains the operator export. Caps stay in `policy/default.json`; do not write
spend there. `RECERTIA_QUOTA_RESERVE_TTL_S` (default 86400) releases stale
reservations. Ledger HMAC when `RECERTIA_LEDGER_HMAC_KEY` is set. Existing
unsigned ledgers still verify until the first signed line. The same key HMACs
seed `version.hmac` files; unsigned seeds still load while the key is empty.
Per-tenant model keys: JSON object in `RECERTIA_TENANT_MODEL_KEYS` or a
mode-0600 file at `RECERTIA_TENANT_MODEL_KEYS_FILE`
(`{"tenant":{"anthropic":"…","openai":"…"}}`). The local executor drops those
secrets from the child environment.

**Compatibility.** Default `POST /v1/runs` remains sync. FastAPI remains an
optional `[api]` extra. Unsigned ledgers still verify until the first signed
line. `--local-exec` is unchanged for development. Container backend remains the
production default.

**Not in this release.** Postgres RLS, KMS envelope encryption, Firecracker /
remote executor, OIDC JWKS signature verify, ledger contract-level sig/key_id
rotation, async-by-default (`RECERTIA_API_DEFAULT_ASYNC=1`), crypto-shred tenant
delete, delta checkpoints.

### Added

- **Tenant-keyed control plane** — runs/events/blobs under `runs/<tenant>/`;
  memberships `tenant_admin`/`operator`; CLI local operator is `platform_admin`.
  `recertia tenant export` (dry-run default; `--execute --dest`).
  [ADR-0021](docs/adr/0021-deployment-model.md).
- **`recertia worker`** — drains the SQLite run queue. `POST /v1/runs`
  `mode=async` enqueues; optional wait (`?wait=`, `X-Recertia-Wait`,
  `Prefer: wait`, cap 30s). `RECERTIA_API_DEFAULT_ASYNC` stays false.
- **`recertia backup verify`** — `MANIFEST.json` sha256. Backup/restore take the
  root lock and refuse while the API holds it.
- **Ledger HMAC** — optional `RECERTIA_LEDGER_HMAC_KEY`. `ledger verify
  --check-sig` / `--check-anchor` (anchor skipped). `ledger rekey --dry-run`
  only. Seed `version.hmac` when the same key is set.
- **Job quota reserve/settle** — `job_quota.sqlite`; JSON sidecar is the
  operator export.
- **Per-tenant model keys** — `RECERTIA_TENANT_MODEL_KEYS` or mode-0600
  `RECERTIA_TENANT_MODEL_KEYS_FILE`. Local executor drops those secrets.
- **`GET /v1/runs` keyset pagination** — `cursor` / `next_cursor` from
  `run_summaries`.
- **Eval outbox** — observation row commits with the checkpoint; drain is
  at-least-once.

- **ADR-0020 Store Receipts (Proposed)** — `StoreReceipt` contract, `close_receipt`
  ledger action, `schema/store_receipt.schema.json`. Does not flip `recertia backup`
  or tabletop defaults. P4 may switch `backup --receipts` only after one counted
  soak week compares tar vs receipt restore. Plan:
  [`docs/plans/2026-09-23-store-receipts.md`](docs/plans/2026-09-23-store-receipts.md).

### Fixed

- **Windows `rss_bytes`** — `import resource` is POSIX-only. Catch `ImportError`
  so hop telemetry on native Windows returns `0` instead of aborting the graph
  (`ModuleNotFoundError: No module named 'resource'`).
- **CI architecture2 drift** — regenerate `docs/architecture2.md` so the Recuris
  `a10` assumption from #47/#48 is compiled. `generate_architecture2.py --check`
  is green again.
- **weekly-ops postgres-soak** — use `pgvector/pgvector:pg16` instead of
  `postgres:16-alpine`. Soak migrations call `CREATE EXTENSION vector`; the
  stock image does not ship it, so every Monday job failed.

### Changed

- **API keys** — stored as HMAC-SHA256 with a pepper. Existing PBKDF2 rows
  rehash on the next successful authenticate. `multi_node` requires
  `RECERTIA_API_KEY_PEPPER`.
- **Console sessions** — ignored when `RECERTIA_CONSOLE_AUTH=off`. Cookie v2,
  `SameSite=Strict`, CSRF double-submit, Origin check. OIDC nonce + `jti`
  (signature verify not landed).
- **Settings** — `RECERTIA_DEPLOYMENT_MODE=single_node|multi_node`. `multi_node`
  refuses SQLite and the local execution backend. `--local-exec` does not write
  process env. API `docs_url` off when `RECERTIA_ENV=prod`. API umask `077`.
- **Bare wheel** — `recertia --help` does not import FastAPI.

- **Console / registry / distill extract** — `register_console_routes` is a
  dispatcher over session / library / program modules (`console_deps.RouteState`
  owns tenant/auth helpers). First-domain tool handlers live in
  `recertia.solver.handlers`; `default_registry` only registers. Success, import,
  and paper distill share `assert_candidate_hygiene` / `assert_non_noop_skill`
  (true-noop steps and `true`/judge-only criteria refused; import/paper still
  `require_clean`). Not a GA gate; no 16th graph node; job CLI/HTTP dispatch is
  unchanged.

- **RW-OR honesty** — remaining-work §8 and the milestone map mark OR1–OR3 as shipped (OG-7…OG-11 tests already on main). Unknown-slug cost uses defaults and is not vendor-exact.

- **2026-08-24 landing extract** — one owner per seam: `ingest_trajectory` /
  `ImportResult.as_public_dict` (CLI + `POST /v1/trajectories/import`),
  `skill_task_class` (snake Goal/golden → kebab `SkillVersion.task_class`),
  imported distill hygiene + true-noop refusal, `ExactMatchTtl` for read-only
  caches (writes still skipped), `hop_finished_attrs` hop gauges, systems/MEA/
  operator-brief `not_established` language. Import still never promotes;
  distill stays candidate-only; MEA and idle offload stay default-off; prefix
  tree stays a view; no 16th graph node. Promotion-with-lift and a live computer
  backend remain open. Not a GA gate.

### Added

- **Trajectory distill HTTP twin** — `POST /v1/trajectories/distill` shares
  `distill_imported` / `as_public_dict` with `recertia trajectory distill`.
  Candidate only; `promoted` is always false. Not a GA gate.

- **arXiv ingest in architecture2** — `docs/architecture/arxiv-ingest.md` is
  compiled into `docs/architecture2.md` (same order as `architecture.md`).


- **2026-08-24 landing extract prompt** — executable Cloud Agent prompt + ADR-0014
  Goal pack for the dual-path extract after today's MEA / trajectory / systems stack.
  Not a GA gate. Plan:
  [`docs/plans/2026-08-24-todays-work-refactor.md`](docs/plans/2026-08-24-todays-work-refactor.md).

- **Systems residency (ADR-0018)** — hop telemetry emits `component_class`,
  `rss_bytes`, `workdir_bytes`, `idle_gap_ms` on `node.finished`; canonical keys
  on `tool.invoked` / `retrieve.queried`; `model.completed` from `ModelClient.complete`.
  Read-only tool-result and retrieval caches (writes never cached; index rebuild
  flushes). Idle workdir offload is hash-checked and **default off**
  (`state_management.idle_offload_enabled`). Prefix-tree is a derived view over
  existing trajectory JSONL. `recertia systems` prints the six-property snapshot.
  Fixture baseline is not a 4.6× claim; golden-class RSS is still unmeasured.

- **External trajectories (ADR-0019 rewrite)** — binds to the shipped
  `TrajectoryImport` contract (ProvenanceBundle from `audited_task_state`, snake
  `bug_reproduction` / `playtest_operator` / `docs_auditor`). No
  `Policy.external_trajectory_import` flag. `recertia trajectory distill` authors
  a **candidate** only (kebab `SkillVersion.task_class`). `POST /v1/trajectories/import`
  is the HTTP twin of the CLI. Reexecutable imports with a criteria snapshot
  queue a *pending* proposal, never approved. `external_computer` is registered
  as an `external` tool and refuses unless isolation flags + allow-list are set;
  even then this build does not open a standing VM. JobQuota
  `computer_use_practice_share` (default 0.15). `recertia systems --brief` prints
  stuck/lift/redundancy with honest "not established". Goldens stay eval-firewalled
  and off the repo-chore promotion gate.

- **MEA graph wiring** — intake binds an `AuditedTaskState` sidecar when three-layer
  activation is true (`policy.mea_enabled` + `Goal.mea_opt_in` +
  `Task.execution_strategy=="mea"`); validate is the fresh-context auditor (CAS);
  the trajectory emitter writes `audited_state_delta` on accept; the ledger notes
  `mea_activation_fallback` only when MEA was requested but incomplete. Default
  single-request path is unchanged (no sidecar, no ledger note, no extra events).
  No new graph nodes. `AuditedTaskState` and `TrajectoryImport` are generated
  into `schema/`. Skill-free golden
  `evals/golden/mea/synthetic-multiphase/` runs under `recertia eval run
  --task-class mea`; observations are eval-firewalled and cannot enter
  `causal_lift`. Not on the repo-chore promotion gate.

- **External trajectories Phase 0** — `recertia trajectory import` validates
  the existing `TrajectoryImport` contract (incomplete provenance / empty
  environment rejected), persists append-only under
  `{runs_root}/runs/{tenant}/imports/`,
  and writes an episodic case. Never promotes. Skill-free computer-use
  goldens `evals/golden/{bug_reproduction,playtest_operator,docs_auditor}/`
  run under `recertia eval run --task-class …`; observations are
  eval-firewalled and cannot enter `causal_lift`. Not on the repo-chore
  promotion gate. Candidate distill, HTTP import, and ADR-0019 landed in
  the Unreleased ADR-0019 rewrite bullet; promotion-with-lift and a live
  computer backend remain open. Plan:
  [`docs/plans/2026-08-22-external-trajectories-and-computer-use-goldens.md`](docs/plans/2026-08-22-external-trajectories-and-computer-use-goldens.md).

- **Variance-aware causal lift** — `RunVariance` on `CausalLiftResult` (std-dev, best,
  worst, best–worst gap); `low_run_count` when independent trials are below
  `Policy.min_independent_runs` (default 5). `EvalStore` exposes Bernoulli vectors
  and per-snapshot rates. `recertia lift` prints the gap and refuses established
  language below the floor (Ye et al. 2026).
- **Faithfulness interventions** — eval-only `empty` / `corrupt` / `irrelevant` /
  `filler` transformers (`recertia.evals.interventions`) and
  `recertia faithfulness`. Trajectory Jaccard + normalized edit-distance, pairwise
  by fixture; zero-trial arms are unscored (`score` is None). `--trials N` writes
  tagged `faithfulness:*` observations through `IntervenedSkillStore` +
  `Retriever.bundle_hook`. Those rows cannot enter lift or contribution samples.
  T3, import-forbidden from nodes/jobs (Zhao et al. 2026). Production omits the hook.
  `Retriever.bundle_hook` is constructor-only (read-only after init). Curator
  persists specificity proposals to `proposals.jsonl` so weekly runs do not
  re-flag the same skill.

- **Applicability gate** — environment (run `ToolRuntime` when present), exact
  locked-criterion match, contagion structural hash (embedding cosine is advisory).
  Distiller injects the environment model and criterion summary. Ledger action
  `applicability_reject`. Distill routes a failing draft to `one_off`. Promote
  requires a non-judge certification criterion.

- **Specificity lint** — `SPEC` / `VAGUE` on drafts; `require_failure_modes` on the
  authoring prior (`ap-2026.08.1`). Warnings only for already-approved seeds.
  Missing preconditions are a SPEC finding. Curator emits specificity-review
  proposals against the active set and does not `lint_reject` approved seeds.

- Ledger actions `lift_report`, `faithfulness_report`, `applicability_reject`.
- **arXiv paper ingestion (Miner)** — `src/recertia/jobs/arxiv.py` Atom client;

  `mine_from_arxiv` proposals with `curation=mined_from_paper`; CLI `--arxiv-id` /
  `--arxiv-query` / `--arxiv-max` on `recertia jobs run mine`; optional `--submit`
  for candidate drafts only. Docs: `docs/architecture/arxiv-ingest.md`.
  Tests: `tests/unit/jobs/test_arxiv_ingest.py`.
- **Curation enum** — `mined_from_paper` added to `contracts/common.py`.
- `recertia soak record` / `recertia soak status` — empty-eval-DB weeks are
  recorded and not counted. Does not declare GA (RW-GA harness).
- Phase-2 portfolio measurement report
  (`docs/architecture/portfolio-measurement.md`).
- ADR-0016 (interval-bounded retirement) and ADR-0017 (version-write budget).
- `charge_version_write` — sole writer of `spent.versions_written`.
- `assemble_bundle` shared by retrieve and the debug query. Affordance flake
  thresholds live on `RetrievalConfig`.
- `GraphOrchestrator(on_finalize=...)` callback. Eval recording moved to the
  composition root so `recertia.graph` no longer imports `EvalStore`.

### Changed

- ADR-0016 non-goals: RW-PC shipped (dual active-set path is gone).
  and `RECERTIA_PORTFOLIO_CONTROLLER` are gone (RW-PC /
  [`portfolio-measurement.md`](docs/architecture/portfolio-measurement.md)).
- Retirement benches on `interval_high < −τ`, not the point estimate (ADR-0016).
  A missing interval cannot retire.
- `budget_excess` includes `versions_written`. Distill / review refuse a write
  that would exceed `max_versions_written`; `store` is the hard stop (ADR-0017).
- Extract Method on the walk: `solve` is a strategy switch into sibling modules,
  `distill` is named honesty gates, `Retriever.search` is stage calls, `_execute`
  is hop / route / snapshot / checkpoint.
- Split `SearchCapability` from `IndexMaintenance`. The retrieve node cannot
  rebuild or upsert. Debug `federated_query` refuses a stale index instead of
  rebuilding it.
- One `retirement_decision` predicate. `propose_retirements` and
  `maybe_bench_on_contribution` are adapters. The Curator job now applies
  proposals; `recompute_active_set` still does not bench.
- `estimate_contribution(..., has_required_non_judge)` is required. Judge-only
  samples produce `estimate is None`.

### Notes

- Paper candidates are retrieval stubs. Promotion still requires the golden gate;
  this path does not claim lift.
- External-trajectory Phase 0 (import CLI + computer-use goldens) must not delay
  RW-GA soak weeks or probe cadence. Distill/promotion is still Phase 1.

## [0.1.0] - 2026-08-15

First public preview. Engineering through M0–M9 is on `main`. Operator-mode GA
(soak weeks, tabletop log, live `repo-chore` metrics) is still open; do not read
this version as production-ready.

### Added

- Contracts-as-code (`contracts/`) with generated JSON Schema (`schema/`)
- Graph runtime, plural memory planes, golden-gated promotion, control-arm lift
- CLI (`recertia`) and optional FastAPI console (Pilot / Tower / Ops)
- Container execution backend (Docker/Podman); `--local-exec` for development
- Seed skills and golden evals under `skills/` and `evals/`
- PolyForm Noncommercial license (`LICENSE`, `NOTICE`), `SECURITY.md`, and `CONTRIBUTING.md`
- Canonical GitHub identity locked to `github.com/recertia/recertia` (clone URL, package
  metadata, and contributor guide). `pyproject.toml` is parsed in CI so a duplicate
  `license` key cannot break install again.

### Notes

Research outcomes `a1`–`a4` stay in [`docs/assumptions.md`](docs/assumptions.md)
until real traffic produces intervals. Remaining ops gates:
[`docs/architecture/remaining-work.md`](docs/architecture/remaining-work.md).
