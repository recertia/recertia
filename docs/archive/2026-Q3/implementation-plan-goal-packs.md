# Goal packs / migration programs — implementation plan (revised)

Build order after principal review of ADR-0014. Normative:
[`specifications/goal-packs.md`](specifications/goal-packs.md). Architecture:
[`architecture/goal-packs.md`](architecture/goal-packs.md). Decision:
[ADR-0014](adr/0014-goal-packs-as-migration-programs.md) (**accepted**).

**Public API name:** `/v1/programs` (`MigrationProgram`). Product copy may say “Goal pack”.
Distinct from Tower `ReplayPack`.

Shipped on `main` via [#50](https://github.com/recertia/recertia/pull/50) (`3dda7dc`).

## Guiding rules (revised)

1. One Goal → one lock → one run remains the execution atom.
2. `freeze_enforcement=hard` is allowed only with digest-sealed `must_not_modify` (GP1 shipped).
   Default remains **`advisory`** for drafts that do not need freeze injection.
3. **GP0 execution requires** `plan_only`, **or** `workdir`, **and/or** `external_handoff`
   (branch / PR / SHA). Empty isolated workspaces are not a migration handoff.
4. **Linear ordinals only** through GP1 (no DAG).
5. **`run_ids[]` + `current_run_id`**; never overwrite attempt history.
6. Persist board before copy-forward / git tip / auto-advance.
7. Auto-advance is **deferred** (not GP2 scope).
8. Types live in `contracts/program.py` (ADR-0009).

## Milestone map (revised)

```text
GP0   Durable linear program board + preview + bind-run     SHIPPED (#50)
GP0.5 Probe + Compose decompositions + from-pack            SHIPPED (#50)
GP1   freeze_enforcement=hard + seal + skip + pack budget SHIPPED (#50)
GP2   git_tip handoff + repo_binding                        SHIPPED
```

| Milestone | Status |
| --- | --- |
| **GP0** | **Shipped** — contracts, store, `/v1/programs`, materialize, stress, bind, Pilot Programs board |
| **GP0.5** | **Shipped** — `POST /v1/goals/probe`, suggest `decompositions[]`, `POST /v1/programs/from-pack` |
| **GP1** | **Shipped** — digest-sealed `must_not_modify`, hard freezes, skip, pack budget check |
| **GP2** | **Shipped** — `git_tip` + registered `repo_binding`; tip record; fresh-workdir checkout |

---

## GP0 — shipped (#50)

### Scope delivered

- `contracts/program.py` — `MigrationProgram`, `MigrationStep`, `ExternalHandoff`,
  `budget_from_goal_constraints`
- `src/recertia/programs/` — store, materialize, stress, GP0 prereqs
- HTTP: create/list/get/accept/abandon, step patch/preview/run (`plan_only` or `bind_run_id`)
- Default `freeze_enforcement=advisory`; bind integrity vs `criteria_preview_hash`
- Linear predecessor gate; idempotent bind; tenant isolation
- Console **Programs** board (list → accept → preview → submit+bind)
- Tests: `tests/unit/test_migration_programs.py`

### Operator flow

1. `POST /v1/programs` with steps → `POST …/accept`
2. `POST …/steps/{id}/preview` → inspect compiled criteria
3. `POST …/steps/{id}/run` (envelope) → `POST /v1/runs` with `run_create`
4. `POST …/steps/{id}/run` with `bind_run_id` (+ workdir or external_handoff)

---

## GP0.5 — shipped (#50)

- `POST /v1/goals/probe` allowlisted read-only inventory
- Suggest returns `decompositions[]`; Compose “Save pack as program”
- `POST /v1/programs/from-pack`
- Single propose surface (`/v1/goals/suggest`)

## GP1 — shipped (#50)

- `seal_must_not_modify_criteria` at intake (`src/recertia/validation/freeze.py`)
- `freeze_enforcement=hard` enabled when sealing is ready
- Step skip with note; pack budget exhaustion fails closed
- Tests: `tests/unit/test_freeze_seal.py`

## GP2 — continuity (shipped)

- Prefer **git_tip** + registered `repo_binding` over whole-tree `copy_forward`
- `POST …/repo-binding` under tenant `repo_bindings/`; reject unbound `git_tip`
- `POST …/steps/{id}/record-tip` records `external_handoff.head_sha`
- `POST …/steps/{id}/seed-workdir` checks out tip into a **fresh** canonical run workspace
- Checkout failure → step `failed` / program `blocked`; no shared live mount; no auto-advance
- Tests: `tests/unit/test_migration_programs.py` (git_tip cases)

## Success metrics (feature health)

- First-bind step success rate
- Share of steps without `generic_pytest_only`
- Freeze-violation catch rate under hard enforcement
- Packs completed without skip

## Out of order / do not do

- Mega-Goal “raise K”
- Suggest auto-submit of all steps
- Shared live workdir across steps
- Pack status as promotion signal
- Auto-advance / DAG (still deferred after GP2 git_tip)
