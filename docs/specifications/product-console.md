# Recertia Specifications: Product console

Normative requirements for the operator/reviewer console. Architecture rationale:
[`../architecture/product-console.md`](../architecture/product-console.md). Decisions:
[ADR-0012](../adr/0012-product-console-surfaces.md).

This document extends — and does not replace —
[`promotion-api-and-observability.md`](promotion-api-and-observability.md) §9–10. Where this
file and §9 conflict on aspirational routes, **this file wins for console milestones C0–C5**.

## 1. Actors and roles

| Role | MAY | MUST NOT |
| --- | --- | --- |
| `operator` | Create/resume/cancel runs; view transcripts; trigger Practice/Curator dry-runs; view metrics | Promote to `approved` without reviewer role (unless policy grants operator+reviewer) |
| `reviewer` | Decide proposals; trigger golden-gated promote; view contribution / replay packs | Disable ablation or edit `MetricReport` numbers |
| `admin` | Manage tenant membership, quotas, OIDC config, break-glass | Bypass golden gate or ledger append |

Service API keys keep existing scopes (`runs`, `blobs`, `metrics`, `exec`, `admin`). Console
sessions MUST carry a role distinct from raw API-key scopes.

## 2. Goal authoring (Pilot)

### 2.1 Form → Goal

The console MUST compile UI state to a valid `Goal` ([goal-objects.md](goal-objects.md)):

- ≥1 required non-judge desired state (`weight ≥ 1.0`)
- Optional constraints (`must_not_modify`, `must_pass_command`, `budget_ceiling`, …)
- `task_class` default `repo-chore` until the operator selects another
- Optional `context` (advisory only)

The console MUST expose a **preview** of compiled Goal JSON and of `compile_goal` criteria
before submit.

### 2.4 Compose suggest (optional assist)

`POST /v1/goals/suggest` MAY return draft `desired` / `constraints` / optional `pack` from
model or heuristics. Responses MUST include a disclaimer that drafts are not locked.
The console MUST require explicit human apply before preview/submit. Suggest MUST NOT write
run manifests or `TaskCriterion` locks.

### 2.2 Templates

v1 SHOULD ship templates that produce Goals equivalent to seed chores (gitignore entry,
pytest config, EditorConfig, …). Templates MUST NOT skip criteria locking or sensitivity
proofs at intake.

### 2.3 Workdir

Default submit uses the sandbox run workspace
(`{api_root}/workspaces/<tenant_id>/<run_id>/`). Absolute host paths MUST NOT be accepted
as raw `workdir` on `POST /v1/runs`.

To bind a real repository, operators register an allowlisted **registered workspace** and
select it in Pilot. Normative contracts (Windows drive-letter roots, registry HTTP,
`workdir.json` kinds, RW-* tests):
[registered-workspaces.md](registered-workspaces.md).

The console MUST refuse paths/shapes that the API would reject (absolute subpaths;
sandbox-incompatible values).

### 2.4 Goal packs (migration programs)

For large refactors the Pilot SHOULD expose a **Goal pack** board: ordered Goals with
dependencies and freeze paths, each submitted as a normal run after human confirm. Suggest
drafts MUST NOT lock criteria. Normative: [goal-packs.md](goal-packs.md). Build order:
[implementation-plan-goal-packs.md](../archive/2026-Q3/implementation-plan-goal-packs.md).

## 3. HTTP API (console)

Auth for browser clients: session or user bearer obtained via OIDC (milestone C3). Until C3,
a development mode MAY use an admin-issued API key held only in a server-side BFF — never in
frontend source.

Unless noted, JSON request/response. Error envelope target:

```json
{ "error": { "code": "budget_exhausted", "message": "...", "run_id": "...", "retryable": false } }
```

### 3.1 Runs

| Method | Path | Purpose | Milestone |
| --- | --- | --- | --- |
| `GET` | `/v1/runs` | List runs for the caller's tenant. Query: `task_class`, `terminal`, `cursor`, `limit` (1–100, default 50). Body `{items, next_cursor}` from `run_summaries` | C0 |
| `POST` | `/v1/runs` | Create run. Body adds optional `mode`: `sync` (default; `RECERTIA_API_DEFAULT_ASYNC` stays false) \| `async`. Async wait (cap 30s): `?wait=`, `X-Recertia-Wait`, `Prefer: wait` | C2 for `async` |
| `GET` | `/v1/runs/{run_id}` | Status, terminal, route_log, spend summary, manifest pins | exists; extend fields C0 |
| `GET` | `/v1/runs/{run_id}/transcript` | Structured transcript (or signed blob redirect) | C0 |
| `GET` | `/v1/runs/{run_id}/trajectory` | Trajectory header + event summary | C1 |
| `GET` | `/v1/runs/{run_id}/events` | SSE stream of run events (see §4) | C2 |
| `POST` | `/v1/runs/{run_id}/resume` | Resume (exists) | exists |
| `POST` | `/v1/runs/{run_id}/cancel` | Cooperative cancel at next node boundary | C2 |

`GET /v1/runs` MUST NOT return other tenants' runs. List items MUST include at least:
`run_id`, `task_class`, `terminal`, `attempt_no`, `created_at`, `cost_usd`, `arm`.

Async create response:

```json
{ "run_id": "...", "status": "queued", "mode": "async" }
```

HTTP status `202` when `mode=async` and no wait completes. Sync mode keeps blocking semantics and `200`. A wait that finishes before the cap returns the terminal record.

### 3.2 Skills

| Method | Path | Purpose | Milestone |
| --- | --- | --- | --- |
| `GET` | `/v1/skills` | List summaries; filter `task_class`, `lifecycle`, `active`; each item includes `live_mix` (`reason`, `eligible`, `consecutive_field_failures`) | C0 |
| `GET` | `/v1/skills/{skill_id}/versions/{version}` | Full version + status + stats plus `identity` (`authoring` from `Provenance`, `applications` from `SkillStats.apply_diversity`) and `live_mix` | C0 |
| `POST` | `/v1/skills/search` | Retrieval debug: query, top-k, scores, drop reasons | C1 |
| `POST` | `/v1/skills/{skill_id}/versions/{version}/promote` | Enqueue golden-gated promote; returns `job_id` | C1 |

Promote MUST NOT set `approved` in the request handler. It MUST invoke the same gate as
`recertia skills promote` and record progress under jobs (§3.4). The caller MUST hold
the `promote` or `admin` API-key scope, or a console session with the `reviewer` role.
A `runs`-only key is not sufficient.

### 3.3 Proposals and reviews

| Method | Path | Purpose | Milestone |
| --- | --- | --- | --- |
| `GET` | `/v1/proposals` | List; filter `status`, `kind` | C1 |
| `GET` | `/v1/proposals/{proposal_id}` | Detail including optional `replay_pack` | C1 |
| `POST` | `/v1/proposals/{proposal_id}/decision` | Body: `{ "decision": "approve"|"reject"|"request_changes", "note": "..." }` | C1 |
| `GET` | `/v1/reviews?status=pending` | In-run / draft review items (if ReviewService wired) | C1 |

Decision semantics:

- `approve` on a proposal MAY enqueue candidate persistence or golden promote depending on
  `kind` and policy tier (T1 vs T2). It MUST append a ledger entry with actor = human id.
- `reject` / `request_changes` MUST NOT delete history; status becomes terminal for that
  proposal id; supersession uses a new proposal id.
- T2 kinds (`correction`, policy) MUST require `reviewer` role.

### 3.4 Jobs

| Method | Path | Purpose | Milestone |
| --- | --- | --- | --- |
| `GET` | `/v1/jobs` | Recent job runs + status | C1 |
| `POST` | `/v1/jobs/{job}/run` | Trigger `mine|curator|practice|recertify|shadow|parallelise|serialise|correction` | C1 |
| `GET` | `/v1/jobs/{job_run_id}` | Status, proposals emitted, errors | C1 |

Body for trigger MAY include the same flags as CLI (`dry_run`, `max_proposals`, hints, …).
Jobs still MUST NOT write `approved` directly. Trigger requires the `jobs` or `admin`
API-key scope, or a console `reviewer` session. `practice` without `one_off` prefers eligible
fail-cluster rows. `recertify` drains the lineage-revoke queue (write-capped). Runner
constructs from `policy/default.json` plus the weekly quota sidecar.

### 3.5 Metrics and ops

| Method | Path | Purpose | Milestone |
| --- | --- | --- | --- |
| `GET` | `/v1/metrics/dashboard` | Exists (telemetry panels + quota) | exists |
| `GET` | `/v1/metrics/report` | `MetricReport` for task class / snapshot (same honesty as `recertia metrics`) | C0 |
| `GET` | `/v1/metrics/canary` | Latest judge canary summary | C0 |
| `GET` | `/v1/ledger/verify` | Integrity verification result | C1 |

### 3.6 Identity (C3+)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/v1/me` | Human identity, roles, tenants |
| `POST` | `/v1/auth/logout` | End session |
| `POST` | `/v1/auth/switch-tenant` | Switch `active_tenant` within membership |

OIDC login routes MAY live on the BFF (`/auth/login`, `/auth/callback`) rather than under
`/v1`. Session cookies are ignored when `RECERTIA_CONSOLE_AUTH=off`. Cookie is v2,
`SameSite=Strict`, CSRF double-submit (`X-Recertia-CSRF`). OIDC nonce + `jti`;
JWKS signature verify is not shipped. C5 tenant-switcher **UI** is not shipped.

## 4. Run events (SSE)

`GET /v1/runs/{run_id}/events` streams `text/event-stream` for callers authorized on that run.

### 4.1 Event envelope

```json
{
  "event_id": "01J…",
  "run_id": "…",
  "type": "node.finished",
  "at": "2026-08-01T22:00:00Z",
  "payload": { }
}
```

### 4.2 Required types (console v1)

| Type | Payload (min) |
| --- | --- |
| `run.queued` | `mode` |
| `run.started` | `task_class`, `manifest` summary |
| `node.started` / `node.finished` | `node`, `attempt_no`, optional `route` |
| `tool.invoked` | `tool`, `ok`, truncated stdout/stderr refs |
| `criterion.scored` | `criterion_id`, `passed` |
| `run.finished` | `terminal`, `cost_usd`, `attempt_no` |
| `run.cancelled` | `by` |
| `error` | `code`, `message`, `retryable` |

SSE MUST support Last-Event-ID (or `?after=`) for reconnect. Events MUST be tenant-scoped.
Payloads MUST NOT include raw secrets; use blob digests for large transcript chunks.

## 5. Durable proposals

### 5.1 Record

A proposal record MUST include:

- `proposal_id`, `kind`, `skill_id`, `version`, `rationale`, `payload`
- `status`: `pending | approved | rejected | request_changes | superseded`
- `created_at`, `created_by_job` or `created_by_run`
- optional `replay_pack`, `git_pr_url`, `decision` (`actor`, `at`, `note`)

### 5.2 Git-native path

When `git_pr_url` is set, the console MUST show PR state as authoritative for merge.
Approving in-app while a PR is open MAY only record intent; applying library bytes still
requires the golden gate / merge path configured for the deployment.

## 6. UX requirements

### 6.1 Pilot

1. Goal form MUST validate client-side constraints that mirror Goal model rules, then
   re-validate server-side.
2. Runs list MUST show terminal and cost without opening detail.
3. Run detail MUST show manifest pins (model, snapshot, library commit) whenever present.
4. Live view MUST degrade to polling `GET /v1/runs/{id}` if SSE unavailable.

### 6.2 Tower

1. Skill browser MUST distinguish `draft|candidate|shadow|approved|benched|quarantined`.
2. Proposal detail MUST render ReplayPack status (`not established` when interval spans 0).
3. Promote UI MUST display failing golden task ids on regression — never a bare boolean.
4. Practice panel MUST label curriculum traffic as excluded from user-facing lift.

### 6.3 Accessibility and safety

- No automatic execution of `exec`-scoped scripts from a Goal template without explicit
  operator confirmation.
- Destructive actions (reject, cancel, bench) REQUIRE confirm affordance.
- Console MUST NOT offer a control that sets `RECERTIA_COMMAND_POLICY=off` without admin
  role + typed confirm.

## 7. Multi-tenant (C5; Phase-4 gated)

Until the production-readiness gate passes:

- Console MAY assume a single tenant.
- APIs MUST still isolate by `tenant_id` on every read path (defense in depth).

After the gate:

- `GET /v1/me` returns multiple tenants; console offers a switcher.
- Skills/facts roots MUST be tenant-scoped or explicitly org-shared with redaction on
  upscope ([scope model](../architecture/measurement-and-scope.md)).
- Cross-tenant proposal and run leakage is a release blocker.

## 8. Non-requirements

- Pixel-perfect design system (choose any coherent system; prefer one expressive font stack
  consistent with product branding guidelines when a brand exists).
- Mobile-first phone layouts in C0–C2 (responsive tablet/desktop is enough).
- In-browser container log multiplexers.
- Editing raw `version.json` without going through proposal + gate.

## 9. Conformance tests (CI)

Milestones MUST add tests that lock:

| ID | Assertion |
| --- | --- |
| PC-1 | `GET /v1/runs` never returns another tenant's `run_id` |
| PC-2 | `POST …/promote` does not set `lifecycle=approved` without golden gate success |
| PC-3 | Proposal `approve` appends a ledger entry with human actor |
| PC-4 | SSE disconnect/reconnect via `after=` does not duplicate terminal event semantics |
| PC-5 | `MetricReport` via `/v1/metrics/report` preserves `unavailable` reasons (no silent zeros) |
| PC-6 | Goal form preview round-trips through `Goal` validation |

## 10. CLI parity

Console features SHOULD gain CLI twins when useful for operators without a browser
(`recertia proposals queue`, `recertia review decide`, …), but CLI parity is not a gate for
C0–C2 HTTP+UI delivery.
