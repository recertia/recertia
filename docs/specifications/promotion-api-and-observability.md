# Recertia Specifications: 8. Promotion policy

## 8. Promotion policy

```text
draft      → candidate : all non-judge criteria passed during the originating run
candidate  → shadow    : task_class has an eval set with >= 5 golden tasks
shadow     → candidate : >= 10 shadow applications
                         AND lift / success thresholds (maybe_advance_shadow_to_candidate)
candidate  → approved  : golden-gated promote_to_approved (human approval is the v1 default path;
                         post-shadow eligibility still requires the same golden gate);
                         self_distilled also needs apply_diversity.distinct_apply_sessions ≥ 2
approved   → deprecated: a newer version of the same skill reaches approved
any        → quarantined: 2 consecutive field failures, or a reviewer rejection
                         (enqueues lineage revoke; Recertifier drains)
```

These are all `SkillStatus` transitions (§2.2, §2.5), made by the Curator or Recertifier reading
across runs — never by a single run's task-plane graph (§4, ADR-0008). Shadow autonomy advances
only to `candidate`; it MUST NOT write `approved`.

Regression gate: before any promotion to `approved`, the golden set for the skill's
`task_class` MUST run green against the candidate via `promote_to_approved`. A regression
blocks promotion. The caller sees a decision-only refusal: the decision, the refusal kind, a
count of failing fixtures, the caller-supplied skill id and version, and an opaque random
audit reference. It MUST NOT carry fixture ids or names, paths, inputs, criteria or expected
outputs. Every failing fixture id, with path and detail, goes only to the operator audit
store (`RECERTIA_OPERATOR_AUDIT_ROOT`); with no usable store the gate MUST NOT run and
promotion is refused. When the candidate supersedes an
approved predecessor, the gate MUST also re-run every golden fixture that predecessor passed
(predecessor non-regression). Failing a predecessor fixture is a refusal even if the
candidate still solves its own fixture.

`promote_to_approved` writes `lifecycle=approved`. It sets `active=True` only for
`human_authored` and `mined_from_human_artifact` skills. `self_distilled` versions remain
`active=False` until contribution evidence is non-negative (bounded shadow slots gather that
evidence). The Recertifier quarantines a version after two consecutive treatment-arm field
failures where that skill was applied.

## 9. HTTP API

Versioned under `/v1`. JSON only. Auth: `X-API-Key` with scoped keys (`runs`, `blobs`, `metrics`, `admin`).

### Implemented (offline + console C0–C4 + remaining-work HTTP)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness |
| `POST` | `/v1/runs` | Execute a task via `GraphOrchestrator.start` (sync; optional `mode=async`). Body: `request` or `goal`, optional `task_class`, `criteria`, `script`, `budget`, `workdir`, `run_id`, `arm`, `model` (console slug; allowlisted server-side). Quota exhaustion and other `/v1` `HTTPException`s return the error envelope. |
| `GET` | `/v1/runs` | List/filter runs (console C0) |
| `GET` | `/v1/runs/{run_id}` | Status / terminal / route log (memory + checkpoint fallback) |
| `POST` | `/v1/runs/{run_id}/resume` | Resume from last checkpoint |
| `GET` | `/v1/runs/{run_id}/transcript` | Structured transcript |
| `GET` | `/v1/runs/{run_id}/events` | SSE run event stream (console C2) |
| `POST` | `/v1/runs/{run_id}/cancel` | Cooperative cancel at next node boundary |
| `POST` | `/v1/blobs` · `GET` `/v1/blobs/{digest}` | Content-addressed blob put/get |
| `GET` | `/v1/metrics/dashboard` | Telemetry dashboard panels |
| `GET` | `/v1/metrics/report` · `/v1/metrics/canary` | Compounding metrics (yield/precision/decay or `unavailable`) and judge canary |
| `GET` | `/v1/skills` | List/filter by `task_class`, `lifecycle` |
| `GET` | `/v1/skills/{skill_id}/versions/{version}` | Full skill version + `identity` split (console C0) |
| `POST` | `/v1/skills/search` | Retrieval debug: scores |
| `POST` | `/v1/skills/{skill_id}/versions/{version}/promote` | Enqueue golden-gated promote (console C1); decision-only refusal, see *Promote responses* |
| `GET` | `/v1/proposals` · `POST` `/v1/proposals/{id}/decision` | Improvement-plane proposals |
| `GET` | `/v1/reviews` · `POST` `/v1/reviews/{decision_id}` | Alias of pending proposals until distill-review volume splits |
| `GET` | `/v1/jobs` · `POST` `/v1/jobs/{job}/run` | Improvement-plane job status and manual trigger (§20). HEX/compress remain gated (RW-6). |
| `POST` | `/v1/evals/runs` | Golden set against a library snapshot (eval firewall; no candidate writes) |
| `GET` | `/v1/facts` · `/v1/cases` · `/v1/cases/{case_id}` · `/v1/affordances` | Tenant-scoped non-procedural memory reads |
| `POST` | `/v1/memory/query` | Federated retrieve debug across planes; does not start a run |
| `GET` | `/v1/policy` · `POST` `/v1/policy/proposals` | Read Policy (no secrets); T2 proposal only — does not apply |
| `GET` | `/v1/models` | Server-side console model allowlist (OG-11). Not shipped in `console/static/`. |
| `GET` | `/v1/ledger/verify` | Verify the integrity chain (§21) |
| `GET` | `/v1/workspaces` · `POST`/`PATCH`/`DELETE` | Registered workspaces (console) |
| `GET` | `/v1/me` · `POST` `/v1/auth/*` | Console session (dev login / OIDC). C5 tenant-switcher **UI** is not shipped. |
| `POST` | `/v1/goals/*` · `/v1/programs/*` · `/v1/templates` | Goal packs / programs (GP0–GP2) |

`POST /v1/runs` is **not** enqueue-only in sync mode: it drives the graph to a terminal (or error) before responding. Async mode returns 202 and streams via SSE.

**Promote responses (REC-009).** `POST /v1/skills/{skill_id}/versions/{version}/promote`
returns `{job_run_id, status: "succeeded", lifecycle, active}` on success. A golden-gate
refusal returns 200 with `status: "failed"`, a decision-only `error`, `decision: "refused"`,
`refusal_kind` (`golden_gate` | `predecessor_non_regression` | `gate_error`), `failing_count`
and an opaque `audit_ref` (`pa-` plus a random id); there is no `failing_fixtures` field. The
promotion is refused **before the gate runs** with 503 and `refusal_kind: "audit_unavailable"`
when the operator audit store (`RECERTIA_OPERATOR_AUDIT_ROOT`) is unset, sits inside the API
root, a tenant runs root or a git checkout, or fails to open; a store write that fails after
the gate is also a 503. An unknown skill or version is 404 `skill version not found`. An
unexpected error is 400 `promotion failed: internal error; operator detail: audit_ref=…`. The
promote job (`GET /v1/jobs`, `GET /v1/jobs/{id}`) carries the same decision fields and is
always finished as `failed` or `succeeded`.

Console-oriented behaviour is specified normatively in [`product-console.md`](product-console.md). **product-console.md wins on conflicts** for C0–C4.

### Aspirational (not implemented)

| Method | Path | Purpose |
| --- | --- | --- |
| C5 UI | tenant switcher chrome | Phase-4 gate only ([remaining-work.md](../architecture/remaining-work.md) RW-C5). APIs already isolate by `tenant_id`. |

Error envelope (used on `/v1/*` `HTTPException` including budget/in-flight `POST /v1/runs`; FastAPI `{detail: ...}` MAY remain on `/health` and 422):

```json
{ "error": { "code": "budget_exhausted", "message": "...", "run_id": "01JD...", "retryable": false } }
```

## 10. CLI

### Implemented

```bash
recertia run --spec task.json [--runs-root .recertia] [--run-id ...] [--ablation]
recertia resume <run_id> [--runs-root .recertia] [--spec task.json]
recertia runs show <run_id> [--route-log]
recertia skills lint [--skills-root skills]
recertia skills search "dependency bump" --explain
recertia skills promote <skill_id> --version N --golden-dir PATH
# or: --golden-root PATH [--require-task-class-gate]
# REC-009: [--audit-root DIR] (default RECERTIA_OPERATOR_AUDIT_ROOT; required, else exit 1
# before the gate) [--operator-detail] (operator only: print the refusal's audit record)
recertia ledger verify [--runs-root .recertia]
recertia lift --task-class repo-chore
recertia keys issue|revoke|list
recertia jobs run curator --dry-run
recertia jobs run practice
recertia jobs run recertify
recertia jobs run mine --hint "docs/runbook.md" --submit
recertia jobs run hex|compress   # no-op unless enablement predicates pass
recertia metrics --task-class repo-chore
recertia probes run --probes evals/probes/repo-chore.json
recertia eval run --task-class repo-chore   # --golden-root/--golden-dir must be inside a git checkout
recertia policy
recertia policy propose retrieval.min_score=0.60 --eval-compare "note"
recertia memory query "dependency bump"
recertia backup [--root .recertia] [--output backups/recertia.tar.gz]
recertia restore backups/recertia.tar.gz --dest .recertia-restore
recertia tabletop <run_id> [--restore-from backups/recertia.tar.gz]
recertia canary [--live]
recertia skills list [--task-class repo-chore] [--lifecycle candidate]
recertia skills show bump-python-dep@3
recertia review queue
recertia review approve <decision_id> --note "..."
recertia facts list --scope project
recertia cases show <case_id>
recertia proposals queue
```

Policy is `policy/default.json` (`RECERTIA_POLICY_PATH`). `practice` without `--one-off`
prefers eligible failure clusters. `recertify` drains the lineage-revoke queue.
`metrics` preserves `unavailable` holes (PC-5). HEX/compress refuse without numeric
`practice_conversion` even if policy flags are true. `policy propose` files a T2
proposal and MUST NOT write `policy/default.json`. `review approve` MUST NOT write
`lifecycle=approved`.

### Aspirational (not implemented)

None. C5 is console UI, not CLI.

## 11. Metrics definitions

Precise definitions, because these numbers decide whether the system works:

| Metric | Definition |
| --- | --- |
| `reuse_rate` | runs with `strategy ∈ {apply, adapt}` ÷ all runs, per task class |
| `first_attempt_success` | runs reaching `distill` with `attempt_no == 1` ÷ all runs |
| `attempts_to_success` | mean `attempt_no` at success; unsolved runs excluded but counted separately |
| `cost_per_solved_task` | Σ `spend.cost_usd` ÷ solved runs |
| `regression_rate` | golden tasks passing on version `N` but failing on `N+1` ÷ golden tasks |
| `retrieval_precision_at_3` | human-labelled applicable candidates in top 3 ÷ 3 |
| `library_yield` | approved skills with ≥1 later application ÷ all approved skills |

`library_yield` is the anti-vanity metric: it goes down when the library grows with
skills nobody reuses.

## 12. Observability

Every node emits a span with `run_id`, `node`, `attempt_no`, `route`, `reason`, and spend
delta. Required structured events: `run.started`, `retrieve.completed` (candidate ids +
scores), `plan.decided` (strategy + reason), `solve.attempt.finished`,
`validate.completed` (result vector), `distill.verdict`, `review.decided`,
`skill.version.written`, `run.finished`.

The `route_log` plus the transcript MUST be sufficient to reconstruct why a run behaved
as it did without re-running a model. Replayability is a hard requirement, not a
debugging nicety: it is what makes the eval harness trustworthy.

Additional required events for the expanded architecture: `criteria.locked` (hash + source),
`failure.classified` (class + evidence), `branch.selected` (winner + margin),
`fact.written`, `case.written`, `proposal.created` (job + kind), `recert.completed`,
`policy.changed` (tier + approver), `ledger.appended`.

Concurrency and merge events, required because a parallel run is otherwise unreadable after
the fact: `step.wave.started` (wave index + step ids + claims held), `resource.conflict`
(claim, blocking step, wait duration), `judge.context.opened` (criterion id + lens + inputs
hash, asserting the solver transcript was not attached), `merge.audited` (expected ids,
received ids, missing ids, action taken).
