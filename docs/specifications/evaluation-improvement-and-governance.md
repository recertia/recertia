# Recertia Specifications: 19. Ablation and eval integrity

## 19. Ablation and eval integrity

| Rule | Detail |
| --- | --- |
| Control sampling | `ablation_rate` (default 0.05), stratified by task class, assigned at `intake` |
| Control behaviour | `retrieve` returns an empty bundle with `suppressed = True`; the run is otherwise identical |
| Exclusions | Never sample tasks with `external` side effects, explicit user-supplied skills, or eval fixtures |
| Rate governance | `ablation_rate` is T3 — the system cannot shrink its own control arm (§22) |
| Eval firewall | Runs whose task is an eval fixture MUST be blocked at `distill` |
| Golden provenance | A golden fixture MUST NOT be created from a run that produced a stored skill |

`causal_lift` for a task class over a window:

```text
causal_lift = first_attempt_success(treatment) - first_attempt_success(control)
```

Reported with a Wilson confidence interval and sample counts. A lift claim whose interval
includes zero MUST be reported as "not established" rather than as an improvement. This
class-level contrast is `RetrievalAblationEffect` (§24.2). Per-skill retirement uses a
separate shadow-versus-suppression `Contribution`; the control arm MUST NOT be reused as a
per-skill `baseline_success`.

## 20. Improvement job contracts

Every job: reads memory and the run store, writes **only** proposals, and is budgeted.

| Job | Trigger | Emits | Hard rule |
| --- | --- | --- | --- |
| `miner` | Manual, or on repository connect | `draft` skills and facts from history, PRs, CI config, runbooks | Mined skills MUST be validated before promotion; merged history is evidence, not certification |
| `curator` | Scheduled, or on library-size or precision-decay trigger | Active-set recomputation, retirement, extract-child, split, tighten-precondition, merge, compact, **parallelise**, **serialise**, and (flagged) **compress** proposals | Every proposal MUST pass the golden-set regression gate; retirement MUST respect the evidence floor (§24.3); compress uses step units and cached-trace LOO (ADR-0015) |
| `practice` | Scheduled, or ≥3 one-offs in a class, or an eligible failure cluster | Practice runs marked `arm="practice"`; optional HEX search may publish a `PatchTemplate` | Excluded from user-facing metrics; separate budget; HEX only under leftover `JobQuota`; default path prefers incremental eligible clusters over one-offs |
| `recertifier` | Schedule, model upgrade, tool version change, child invalidation, lineage-revoke queue, consecutive field failures | Recert results; `needs_recert` / `quarantined` transitions on `SkillStatus` | MUST re-run sensitivity proofs, not just criteria; MUST re-derive resource claims when a tool's registry entry changes; marking `quarantined` is a `SkillStatus` write, never a task-plane route (§2.5, ADR-0008); revoke drain is write-capped; `record_dead_end` MUST NOT enqueue; two consecutive treatment-arm field failures (skill applied, non-fixture) quarantine via this job |
| `correction_miner` | ≥N reviewer edits accumulated | Distiller-guidance and criteria-template proposals (T2) | MUST NOT self-apply; human approval plus eval comparison required |

Practice task selection targets estimated success probability in `[0.2, 0.8]`, using
`predicted_success` calibrated against outcomes: outside that band an attempt yields little
information, which is the whole point of a curriculum.

The two step-graph proposals are the Curator's half of the concurrency story, and both must
argue from run evidence rather than from reading the skill:

| Proposal | Evidence required | Effect |
| --- | --- | --- |
| `parallelise` | An `input_bindings` entry whose bound input was unused across ≥5 runs, and whose steps' claims do not overlap (§26.1) | Removes the binding, producing a new skill version |
| `serialise` | ≥2 `merge` verdicts or a resource conflict rate above `conflict_threshold` (default 0.1) on the same wave | Adds an edge or widens a claim to `exclusive`, producing a new skill version |

Both go through the normal promotion gate, so a wrongly removed edge shows up as a golden-set
regression before it reaches a user's run. A `serialise` proposal MUST NOT be blocked on the
latency regression it causes: correctness outranks the parallelism metric.

### 20.1 Policy and `JobQuota`

[`policy/default.json`](../../policy/default.json) is the versioned T2 `Policy` document
(`RECERTIA_POLICY_PATH` overrides). `ImprovementFlags` (including `deterministic_guide`,
`practice_hex_search`, `curator_compress`) MUST NOT change graph topology. Caps live on
`Policy.job_quota`; weekly spend is reserved then settled in `job_quota.sqlite`
and MUST NOT be written back into the policy file. The JSON sidecar
(`job_quota.json`) is the operator export of settled spend. ISO week in UTC is
the rollover key.

Priority for `can_admit`: `recertifier` → `curator_retire` → `fail_cluster_author` →
`practice_band` → `practice_hex` (≤ `hex_share` of leftover) → `compress`. Operator names
`recertify` / `practice` map onto those priorities. HEX and compress stay off until
`practice_conversion` and a weekly lift interval exist.

### 20.2 Lineage revoke and failure clusters

`write_version` records authoring sources into `lineage.idx.json` (point map; `lineage.jsonl`
is WAL). `rebuild` from persisted versions is the T0 recovery path.

A transition *into* `quarantined` enqueues `skill:{id}@{version}` plus each
`provenance.source_*` id. Recertifier drain marks intersecting versions and pinning parents
`needs_recert`, capped by `max_status_writes_per_tick`.

`record_dead_end` upserts a `FailureClusterRow`. Practice reads `eligible`.
`cluster_dead_ends` remains only as a rebuild when the incremental index is empty.
Success-path `distill` MUST NOT scan for clusters.

## 21. Provenance ledger

```python
class LedgerEntry(BaseModel):
    seq: int
    prev_hash: str
    entry_hash: str            # sha256 over canonical entry minus entry_hash
    actor: str                 # run id, job name, or human id
    action: Literal[
        "write",
        "advance_to_candidate",
        "quarantine_version",
        "deprecate",
        "policy_change",
        "lint_reject",
        "compress_skill",
        "revoke_lineage",
        "compose_block",
        "publish_patch_template",
        "lift_report",
        "faithfulness_report",
        "applicability_reject",
    ]
    target: str                # skill version, fact id, policy version
    evidence: dict             # criteria results, eval ids, approver
    at: datetime
```

`GET /v1/ledger/verify` recomputes the chain. `recertia ledger verify --check-sig`
verifies HMAC signatures when `RECERTIA_LEDGER_HMAC_KEY` is set; unsigned entries
are allowed until the first signed one. `--check-anchor` is reserved and skipped.
`recertia ledger rekey --dry-run` counts unsigned vs signed lines and does not
rewrite history. Because the system writes its own memory, "who wrote this and on
what evidence" must be tamper-evident rather than merely logged.

## 22. Governance of mutable surfaces

Every mutable surface carries a tier (see [ADR-0005](../adr/0005-self-modification-boundary.md)).

| Tier | Surfaces | Write path |
| --- | --- | --- |
| T0 | Trust scores, affordance aggregates, cases, retrieval caches, `SkillStats.apply_diversity`, failure-cluster rows, lineage idx, weekly `JobQuota` spend sidecar | Runs / jobs write directly; derived and rebuildable |
| T1 | Skill and fact versions, curator proposals, shadow promotions | Promotion policy with eval evidence and zero regressions |
| T2 | Authoring prior, distiller guidance, criteria templates, retrieval thresholds, routing ladder, budget defaults, values of `active_cap` / `retirement_threshold` / `evidence_floor` / `max_parallel_steps` / `conflict_threshold` / `layer_threshold`, `ImprovementFlags` / `ImprovementLimits` / `JobQuota` caps | Versioned config, human approval, eval comparison |
| T3 | Tool registry with side-effect classes **and declared resource claims**, sandbox policy, promotion thresholds, ablation rate, graph topology, judge-isolation and merge-audit enforcement, finiteness of the active cap and retirement threshold, tier assignments | Code or config review only; unreachable from run and job code paths |

Enforcement requirements:

- T3 surfaces MUST be unreachable from any module a run or job can import; asserted by a CI
  import-boundary test, not by convention.
- A write to a T2 surface without a recorded human approver MUST fail closed.
- Any new mutable surface MUST be assigned a tier; an untiered surface is a review blocker.
- Retrieval and prompt content MUST NOT be able to alter a tier assignment — memory is data,
  never instructions (§13.1, and the injection control in `architecture/risk-and-governance.md`
  §15.2).

## 23. Additional metrics

| Metric | Definition |
| --- | --- |
| `causal_lift` | Treatment minus control `first_attempt_success`, with Wilson interval (§19) |
| `calibration_error` | Brier score of `predicted_success` against outcome |
| `abstention_precision` | Abstentions later confirmed unsolvable or human-escalated ÷ all abstentions |
| `recert_pass_rate` | Skills passing scheduled recertification ÷ skills recertified |
| `retrieval_decay` | Change in `retrieval_precision_at_3` per 100 skills added |
| `mean_composition_depth` | Mean `uses` depth of applied skills; rising means abstraction is happening |
| `dead_end_avoidance` | Runs where a retrieved dead end suppressed a repeated approach ÷ runs with dead ends retrieved |
| `curator_yield` | Curator proposals approved ÷ proposals raised |
| `fact_contradiction_rate` | Open contradictions ÷ total facts |
| `practice_conversion` | Practice runs producing an approved skill ÷ practice runs |
| `lint_block_rate` | Drafts failing packaging lint ÷ drafts proposed |
| `distill_fail_path_share` | Failure-cluster *job* drafts ÷ all drafts (not per-run distill) |
| `off_intent_activation` | Runs where `chosen` ∉ `bundle.skills` ÷ applications |
| `guide_used_rate` | Treatment runs where `execution_guide` was set ÷ treatment runs with skills |
| `compose_block_rate` | Store/promote blocked by compose lint ÷ candidates reviewed |
| `practice_hex_accept_rate` | Practice patches that become byte-distinct validation bests ÷ candidates |

`retrieval_decay` is the early-warning metric for library entropy: it turns negative before
`first_attempt_success` does, which is what gives the Curator time to act.

| Metric | Definition |
| --- | --- |
| `skill_contribution` | Per-skill `ĉ(s)`: shadow success rate minus this skill's suppressed success rate (§24.2) |
| `active_cap_pressure` | Task classes at `active_cap` ÷ task classes with skills |
| `retirement_reversal_rate` | Benched versions later restored to `approved` ÷ versions benched |
| `curation_gap` | First-attempt success of `human_authored` + `mined_from_human_artifact` skills minus `self_distilled` skills, per task class |

`curation_gap` exists to test a specific external finding in our own domain (`references.md`
§1.1). If it is near zero here, the higher evidence bar on self-distilled skills should be
relaxed; if it reproduces, the Miner deserves more investment than the distiller.

Concurrency and verification integrity:

| Metric | Definition |
| --- | --- |
| `merge_gap_rate` | Merge audits with missing inputs ÷ merge audits — the rate at which work is dispatched and silently lost; target zero |
| `merge_recovery_rate` | Incomplete merges resolved by re-dispatch ÷ incomplete merges |
| `resource_conflict_rate` | Step waves that blocked on a claim ÷ waves with ≥2 steps |
| `parallel_speedup` | Serial step duration ÷ observed wall-clock duration per skill; the only justification for step DAGs, so it is reported per skill, not in aggregate |
| `fake_edge_rate` | `input_bindings` unused at runtime ÷ declared bindings — leftover serialisation after store-time edges are already data-carrying by construction |
| `judge_isolation_violations` | Judge invocations whose context hash included solver-transcript content; a non-zero value is a release blocker, not a metric to trend |

`parallel_speedup` and `merge_gap_rate` are read together or not at all. Speedup with a
non-zero gap rate is not speed, it is a system that finishes early by dropping work.
