# Recertia Specifications: Goal packs (migration programs)

Normative requirements for **Goal packs** / **migration programs**: ordered Goals for
large refactors. Complements [goal-objects.md](goal-objects.md). Decision:
[ADR-0014](../adr/0014-goal-packs-as-migration-programs.md).

**HTTP resource:** `/v1/programs` (`MigrationProgram` in `contracts/program.py`).  
**Product copy:** “Goal pack”. **Not** Tower `ReplayPack`.

**Non-goals:** mutating criteria after lock; intra-run fan-out as a substitute; Suggest
auto-lock; auto-advance between steps (deferred).

---

## GP-1 Definitions

| Term | Meaning |
| --- | --- |
| **Migration program** | Tenant-scoped linear program of Goal steps (`MigrationProgram`). |
| **Pack step** | One `MigrationStep` with a Goal, freeze/mutate hints, optional `external_handoff`. |
| **Handoff** | Continuity mode: `none` + external git metadata, `operator_workdir`, or `git_tip` (+ `repo_binding`). `copy_forward` deferred. |
| **Program bar** | Embedded `DesiredState` / `Constraint` lists merged into later steps at materialize. |
| **Freeze enforcement** | `advisory` (GP0 default) or `hard` (injects `must_not_modify` only when honest). |

---

## GP-2 Invariants (MUST)

1. Atomic lock unit is the **run** (ADR-0003 / ADR-0010).
2. AI propose / human apply; accept and bind are explicit.
3. Tenant isolation (PC-1).
4. Golden gate unchanged.
5. Step `succeeded` iff bound run terminal ∈ `acceptance_gate.terminal_in`.
6. **Honesty:** `freeze_enforcement=advisory` MUST NOT inject `must_not_modify`. UI/API MUST
   surface `freeze_advisory` when freeze_paths are set under advisory mode.
7. **GP0 execution:** creating/binding a run for a step with `handoff=none` REQUIRES
   `plan_only=true`, **or** a `workdir`, **and/or** populated `external_handoff`
   (branch / pr_url / sha). Empty fresh workspaces alone are insufficient.

---

## GP-3 Data model

See `contracts/program.py`. Summary:

### `MigrationProgram`

`program_id`, `tenant_id`, `title`, `intent`, `task_class`, `decomposition`, `status`,
`steps[]`, `program_bar_desired[]`, `program_bar_constraints[]`, `handoff`,
`freeze_enforcement` (default `advisory`), `repo_binding`, `budget`, `source`,
`disclaimer_acked_at`, `version`.

`version` is an integer the store assigns: 1 on the first save, plus 1 on every later
save. `GET` saves only when the step-status refresh changed something, so reading a
program does not move its version; a step preview saves only when its
`criteria_preview_hash` changes. `GET` and a successful `PUT` on `/v1/programs/{id}`
send the version as a strong `ETag`, for example `ETag: "3"`.

Note (2026-10-30): every other program route whose 2xx response has a top-level `program`
also sends that program's version as `ETag`: create, `from-pack`, `accept`, `abandon`,
step `PATCH`, `skip`, `record-tip`, `repo-binding`, and a step `run` that binds or
replays. A plan-only `run`, the step preview and error responses send none. Only `PUT`
checks `If-Match`.

### `MigrationStep`

`step_id`, `ordinal` (unique, linear), `title`, `role`
(`characterization`|`structural`|`behaviour_lock`|`custom`), `goal`, `freeze_paths`,
`mutate_paths`, `acceptance_gate`, `status`, **`run_ids[]`**, **`current_run_id`**,
`criteria_preview_hash`, `external_handoff`, `goal_revision`.

Characterization is a **step role**, not a separate field list.

### Merge rules (program bar)

Append-by-id. Identical bodies for the same id are OK. Different bodies for the same id
MUST fail materialize (`MaterializeError`).

---

## GP-4 Lifecycle (linear)

```text
draft --accept--> active
  step planned -> ready when ordinal-1 succeeded|skipped (or ordinal==0)
  preview -> POST /v1/runs -> bind_run_id
  succeeded unlocks next; failed -> program blocked
  all required succeeded|skipped -> completed
```

Goal PATCH forbidden when step status ∈ `{queued, running, succeeded}`.  
Bind is **idempotent** for the same `run_id` / `idempotency_key`.

DAG `depends_on` is reserved; GP0 runtime ignores it and uses ordinal-1 only.

---

## GP-5 Stress codes

| Code | Severity | Notes |
| --- | --- | --- |
| `freeze_advisory` | info | freeze_paths present under advisory enforcement |
| `freeze_mutate_overlap` | block | same path in freeze and mutate |
| `vacuous_command` | block | |
| `generic_pytest_only` | warn | |
| `weak_characterization` | warn | structural without prior characterization role |
| `program_bar_dropped` | warn | later step, empty program bar |
| `mega_goal` | warn | prefer program over one huge step |
| `no_hard_criteria` | block | |
| `missing_repo_binding` | block | `handoff=git_tip` without registered `repo_binding` |
| `missing_handoff` | warn | `copy_forward` deferred; use `git_tip` |

---

## GP-6 Workspace handoff

| Mode | Status |
| --- | --- |
| `none` | **Shipped** — external git metadata and/or operator `workdir`; else `plan_only` |
| `operator_workdir` | **Shipped** — `workdir` required to execute |
| `copy_forward` | Deferred; prefer `git_tip` |
| `git_tip` | **Shipped (GP2)** — requires registered `repo_binding`; checkout tip into fresh workspace |

---

## GP-7 Budgets

`budget_from_goal_constraints` applies Goal `budget_ceiling` onto run `Budget` at preview /
materialize. Pack-level remaining budget fails closed on step run (**shipped** GP1).

---

## GP-8 HTTP API

| Method | Path | Purpose | Status |
| --- | --- | --- | --- |
| `POST` | `/v1/programs` | Create draft | Shipped |
| `GET` | `/v1/programs` | List (tenant) | Shipped |
| `GET` | `/v1/programs/{id}` | Detail + refresh ready | Shipped |
| `PUT` | `/v1/programs/{id}` | Replace a draft's editable fields; needs `If-Match` | Shipped |
| `POST` | `/v1/programs/{id}/accept` | draft → active (disclaimer) | Shipped |
| `POST` | `/v1/programs/{id}/abandon` | Abandon | Shipped |
| `PATCH` | `/v1/programs/{id}/steps/{step_id}` | Edit while planned/ready/failed | Shipped |
| `POST` | `…/steps/{step_id}/preview` | Materialize + compile; no lock | Shipped |
| `POST` | `…/steps/{step_id}/run` | `plan_only` / envelope **or** `bind_run_id` | Shipped |
| `POST` | `…/steps/{step_id}/skip` | Skip with note | Shipped |
| `POST` | `/v1/programs/from-pack` | Compose pack → durable draft | Shipped |
| `POST` | `/v1/goals/suggest` | Drafts + `decompositions[]` | Shipped |
| `POST` | `/v1/goals/probe` | Read-only inventory | Shipped |
| `POST` | `/v1/programs/{id}/repo-binding` | Register allowlisted repo | Shipped (GP2) |
| `POST` | `…/steps/{step_id}/record-tip` | Record git tip after success | Shipped (GP2) |
| `POST` | `…/steps/{step_id}/seed-workdir` | Checkout tip into fresh run workdir | Shipped (GP2) |

Bind body: `{ plan_only, workdir, budget, bind_run_id, idempotency_key }`.

**`idempotency_key` on bind.** The key is kept in `control.db` (table `idempotency_keys`,
control migration `003_idempotency`), so a retry after a restart, or from another process
on the same root, gets the first answer back. It applies only to a bind
(`bind_run_id` set, `plan_only` false); a plan-only or unbound call ignores it. A key is 1
to 255 printable ASCII characters (`0x20`-`0x7E`); anything else is 422. A key belongs to
one tenant, program and step, each kept in its own column. The request hash covers the
whole body except the key, with defaults filled in.

A keyed bind writes in this order: (1) a `pending` row in `control.db`, (2) the bind in
`programs.sqlite`, (3) the row marked `complete` with the bound `run_id`. Each control.db
write is one `BEGIN IMMEDIATE` transaction. If (2) fails, the pending row is deleted and
the error is returned. No response is stored: a replay answers from the program as it is
now.

The row's `created_at` is its holder's lease. A takeover sets a new `created_at` (and
`expires_at`), and (3) and the delete after a failed (2) only match the lease they were
given, so a request that was slow rather than dead cannot complete or delete the row a
retry took over. Such a request gets 409 `idempotency_in_progress` once its bind lands,
and a retry then gets the replay; if the row already records the same `run_id`, (3)
succeeds. A row that is gone, or records another `run_id`, when (3) runs is 503
`state_corrupt`.

Note (2026-10-30): a keyed bind also gets 503, binding nothing, when the runtime has no
idempotency store open (`unavailable`), when its reservation carries no lease
(`state_corrupt`), or when opening the store finds a changed control migration
(`state_corrupt`).

| Stored row for the same tenant, program, step and key | Result |
| --- | --- |
| none, or expired | the bind runs; 200 with `idempotent: false` (or `true` if the step already has that run) |
| other request hash | 409 `idempotency_mismatch`; nothing written |
| same hash, `pending`, younger than 30 s | 409 `idempotency_in_progress`, `retryable: true`, `Retry-After: 1` |
| same hash, `pending`, 30 s or older | taken over: the request that held it died between (2) and (3); the bind runs again, or the row is completed if the step already has that run |
| same hash, `complete`, step's `current_run_id` is the stored `run_id` | 200 with `idempotent: true` |
| same hash, `complete`, the step has moved on | 409 `conflict` (`idempotency key already applied; step has moved on`) |

Rows expire `RECERTIA_IDEMPOTENCY_TTL_SECONDS` after they are written (default 86400; 60
to 2592000; any other value is a `ConfigError` when the API starts). An expired row is
never honoured. Expired rows are deleted only by a keyed bind, at most 500 per bind,
inside its step (1) transaction; nothing sweeps at startup and reads write nothing.

**`PUT` and `If-Match`.** The body has the `POST /v1/programs` fields; status, owner,
timestamps, `repo_binding` and `budget` stay as stored. `If-Match` carries the `version`
the client last read (the `ETag` of its last `GET` or `PUT`), as `"3"` or `3`. An unknown
program is 404 before a missing `If-Match` is checked; a malformed `If-Match` is 400
regardless. The version is checked before the status, and only `draft` programs can be
replaced:

| `If-Match` | Result |
| --- | --- |
| absent or blank | 428 `precondition_required` |
| not a version (`*`, a weak tag, a list) | 400 `bad_request` |
| not the stored version | 412 `precondition_failed` (`details.stored_version`) |
| the stored version, program not `draft` | 409 `conflict` |
| the stored version, program `draft` | 200; the saved program with `version` + 1 and its `ETag` |

The parser is deliberately stricter than RFC 9110 §13.1.1: it accepts a bare `3` as well
as the entity-tag `"3"`; it refuses `*` (which would match any version) and lists of tags
with 400; and it answers a weak tag such as `W/"1"` with 400 rather than 412, since the
API never sends weak tags.

The service checks `If-Match` against the version it read, and the store compares that
same version and writes in one transaction. So of two `PUT` requests that read the same
version exactly one saves, and a write that lands between a `PUT`'s read and its save
makes the `PUT` fail with 412. A rejected `PUT` writes nothing. The other program write
routes (accept, abandon, step patch, preview, bind, skip, repo binding, record tip, seed
workdir) still save without a version check; they bump `version` too, so concurrent
writes through them can still overwrite each other.

---

## GP-9 Console

Pilot **Programs** board and Compose (suggest / save pack as program) **shipped** (#50).
GP2 tip SHA / binding status on the board may follow as a client polish.

---

## GP-10 Anti-patterns

- Advertising freeze as enforced under `advisory`
- Binding step N before N-1 succeeded
- Auto-advance
- Pack completion as skill promotion
- Whole-suite pytest as sole structural proof

---

## GP-11 Conformance (GP0)

| ID | Requirement |
| --- | --- |
| GP-T1 | Accept creates zero runs |
| GP-T2 | Preview does not create runs / lock |
| GP-T3 | Bind records `run_ids` / `current_run_id`; linear gate |
| GP-T4 | Dependent step cannot bind while predecessor failed/pending |
| GP-T5 | Tenant isolation |
| GP-T6 | `freeze_mutate_overlap` blocks materialize |
| GP-T9 | `budget_ceiling` applied at materialize/preview |
| GP-T10 | Program bar appears in materialized later steps |

GP-T7/T8 (copy-forward / pack budget) are later milestones.

Covered by `tests/unit/test_migration_programs.py`.
