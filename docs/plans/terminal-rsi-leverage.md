# Recertia × Terminal-RSI Leverage Plan and Specification

Status: Implementation-ready (Phase 0 entry).  
Date: 2026-11-08.  
Scope: Non-parametric leverage of Terminal-RSI (arXiv:2610.22192) into Recertia.  
Non-goals: Model weight updates, critic/PPO training, changes to T3 surfaces.

---

## 1. Vision

Recertia remains a measurement-honest, non-parametric self-improving agent system whose competence compounds only through versioned, re-certified memory that demonstrably lifts control-arm performance. The next increment hardens trajectory hygiene and progress scoring so that only fresh, leakage-screened, verifier-accepted work becomes candidate memory, partial progress cannot inflate apparent success, and post-update recalibration prevents stale validators from propagating. Model weights stay frozen. The three planes and the T0–T3 boundary remain inviolable.

## 2. Assumptions

These are declared, not implied. If any is false, reopen the matching ADR before Phase 1 coding.

| ID | Assumption | If false |
|----|------------|----------|
| A1 | Recertia already has episodic, procedural, measurement, and policy stores, an integrity ledger, a quality gate, and a retrieval-index rebuild. | Do not invent a second memory plane. Map each artifact onto the existing store that already owns that lifetime. |
| A2 | Distill and trajectory-import are the only writers that turn a trajectory into a SkillVersion candidate. | HygieneGate hooks only those two entry points. |
| A3 | Solve already runs in an OCI sandbox with a declared sandbox policy. | HygieneGate reuses that sandbox. No new privilege. |
| A4 | The integrity ledger is hash-chained and append-only. A head identifies a complete prior system state. | Rollback and replay are blocked until the ledger can do this. |
| A5 | Promotion is the only path that adds a SkillVersion to the active retrieval set. | RecalibrationPass attaches to that path. It does not grow a second promotion path. |
| A6 | Control-arm lift (`memory on` vs `memory off`) is the only number that authorizes promotion or retirement. | Calibrated scores never become a promotion gate. |
| A7 | CalibConstants and the turn-decay constant are T2 (human-gated policy). Ablation rate, judge isolation, active-cap finiteness, and the tool registry are T3. | Import-boundary tests fail the build if improvement-plane code writes those surfaces. |
| A8 | Fresh re-execution is the dominant new cost. Sandbox pool capacity is finite and measurable. | Phase 1 does not start until the capacity gate in §12 is green. |
| A9 | One owner operates the system. There is no multi-tenant isolation requirement in this increment. | No cross-tenant design work. |
| A10 | Existing golden sets and the soak CLI remain the measurement harness. | New scores are overlays. Raw check results stay stored unchanged so historical lift stays replayable. |

## 3. Current state

Recertia already enforces retrieve-before-invent, criteria locked at intake, versioned plural memory, control-arm lift, a bounded active set with measured retirement, plane separation, and an integrity ledger. Distill and trajectory import emit candidates that pass a quality gate before promotion. Negative knowledge is first-class. An eval firewall and mutation testing of validators are designed in.

What is missing, and only this:

- Trajectory acceptance trusts the originating run’s verifier. There is no mandatory fresh re-execution plus leakage screen before a candidate is proposed.
- Contribution and retirement scoring treat partial check passes and strict success too similarly. Large-verifier families can dominate score mass.
- Multi-turn attribution is final-outcome or step-count based. Long observation logs distort which fragments are kept.
- Re-certification exists, but there is no short validator-only pass that must succeed before a new snapshot becomes retrieval-eligible.
- Retained-versus-discarded and process-versus-strict views are not a first-class operator surface.

These are hygiene and measurement-fidelity gaps. They are not a missing core loop and they are not a reason to train weights.

## 4. Architectural principles

1. No weight updates. Improvement stays representational.
2. Criteria stay pre-registered. Their hash is an input to hygiene, not an output.
3. Control-arm lift is the only promotion authorization.
4. Fresh execution plus leakage screening is a hard gate for any trajectory-derived candidate.
5. Partial progress is scored and never substitutes for strict terminal success.
6. A snapshot is retrieval-eligible only after RecalibrationPass passes.
7. Improvement-plane code cannot write T3. Import-boundary tests enforce this.
8. Screened artifacts are episodic. They are not skills. Only a promoted SkillVersion enters retrieval.
9. Every new write is ledgered, versioned, attributable, and revertible.
10. The smallest change that unlocks the next increment is this path. No new plane, no new runtime.

## 5. Requirements and traceability

Must requirements. Each maps to one solution element and one interface.

| ID | Requirement | Solution | Interface |
|----|-------------|----------|-----------|
| R1 | A trajectory-derived candidate is proposed only after fresh re-execution against the locked criteria. | HygieneGate | `hygiene_gate` |
| R2 | Private guidance, system-prompt fragments, host paths, and non-public env values are a hard reject. | LeakageScreen | `screen_leakage` |
| R3 | Rejected trajectories are stored as negative knowledge and do not consume the active-set cap. | Episodic write on failure | `HygieneResult` + episodic store |
| R4 | Accepted work is a ScreenedArtifact, not a SkillVersion, until distill and both gates succeed. | Store split | episodic vs procedural |
| R5 | Partial progress cannot dominate contribution or retirement ranking. | ProgressCalibrator | `family_dense_reward`, `success_prioritized_reward` |
| R6 | Strict success remains the promotion gate. Calibrated scores do not. | Quality gate unchanged | existing promotion |
| R7 | Failure-score share on the golden corpus drops by at least 15 percentage points under the new scorer, with strict-success rank order unchanged. | Offline calibration | `CalibConstants` |
| R8 | Contribution credit decays across turn or step boundaries, not across tokens. | StepBoundaryAttributor | `attribute` |
| R9 | A library or policy batch cannot become retrieval-eligible until a validator-only pass succeeds. | RecalibrationPass | `run_recalibration` |
| R10 | The prior retrieval index stays live until the new index rebuild commits. | Two-phase index commit | `commit_index` |
| R11 | Any ledger head replays the accepted artifacts, constants version, manifest, and credits that were live at that head. | StageLedger + integrity ledger | `StageEntry` |
| R12 | Control-arm first-attempt success does not regress after Phase 3. A regression is an automatic rollback, not a tuning pass. | Soak + rollback runbook | §13 |
| R13 | Concurrent hygiene of the same trajectory and criteria hash is idempotent. | Conflict rule | §9.4 |
| R14 | HygieneGate rejects 100% of the leakage fixture suite and accepts 100% of the known-clean suite. | Fixtures | Phase 0 / Phase 1 exit |
| R15 | No new module imports a T3 configuration surface. | Import-boundary tests | CI |

Should requirements, not Phase 0 blockers:

| ID | Requirement | Phase |
|----|-------------|-------|
| S1 | Operator console shows process score and strict completion side by side. | 3 |
| S2 | Low-value task classes may be sampled rather than fully re-executed, after the capacity gate is measured. | 4, only if A8 pressure is real |
| S3 | Borderline leakage rejects can be queued for a human review during soak. They never auto-promote. | 1 soak |

## 6. Target architecture

```text
execution plane
  solve  --writes-->  episodic trajectory
  retrieve  --reads-->  active SkillVersions only

improvement plane
  HygieneGate  --reads-->  trajectory + locked criteria
               --executes-->  fresh sandbox
               --writes-->  ScreenedArtifact | negative case + ledger
  Distill      --reads-->  ScreenedArtifact
               --writes-->  SkillVersion candidate (not active)
  RecalibrationPass --reads-->  candidate snapshot
                    --writes-->  RecalibrationManifest
  Promotion    --requires-->  quality gate AND passed manifest
               --then-->  two-phase index commit

measurement plane
  ProgressCalibrator, StepBoundaryAttributor, StageLedger
  retirement reads scores; it does not promote

policy store (T2, human-gated)
  CalibConstants, gamma_turn
```

### Component responsibilities

| Component | Responsibility | Plane | Owns |
|-----------|----------------|-------|------|
| HygieneGate | Fresh re-execution, leakage screen, acceptance | Improvement | Acceptance predicate, ScreenedArtifact or negative case |
| LeakageScreen | Pure scan of actions and observations | Improvement | Reject reason codes |
| ProgressCalibrator | Family-normalized dense score and success-prioritized score | Measurement | Nothing durable; reads T2 constants |
| StepBoundaryAttributor | Turn-boundary credit | Measurement | StepCredit rows |
| RecalibrationPass | Validator-only pass; blocks retrieval eligibility | Improvement | RecalibrationManifest |
| StageLedger | Retained vs discarded, process vs strict | Measurement | Append-only StageEntry |
| IndexCommit | Two-phase retrieval index swap | Memory | Active index pointer |

## 7. Data ownership

| Artifact | Writer | Readers | Store | Lifetime |
|----------|--------|---------|-------|----------|
| ScreenedArtifact | HygieneGate, accept path | Distill, StageLedger | Episodic | Until distilled or snapshot discarded |
| Negative case | HygieneGate, reject path | Retrieve (negative only), StageLedger | Episodic | Existing dead-end retention |
| SkillVersion candidate | Distill | Quality gate, RecalibrationPass | Procedural, inactive | Until promote or discard |
| Active SkillVersion | Promotion only | Retrieve | Procedural, active | Until retirement |
| CalibConstants | Offline calibration, human gate | ProgressCalibrator | Policy (T2) | Versioned; prior versions retained |
| StepCredit, ProgressScores | Attributor, Calibrator | Retirement, console | Measurement | Soak window + one re-certification cycle, then archive |
| RecalibrationManifest | RecalibrationPass | Promotion | Improvement ledger | For the life of the snapshot |
| StageEntry, receipts | All new writers | Replay, soak, console | Integrity ledger | Never mutated; archive is an operator action |

Writers that are forbidden:

- HygieneGate must not write a SkillVersion or the active index.
- ProgressCalibrator must not write criteria, the ablation rate, or the promotion threshold.
- Retirement must not promote.
- No new module writes the active index except IndexCommit, and only after a passed manifest.

## 8. Schemas

### 8.1 ScreenedArtifact

Stored in the episodic store. The trajectory payload is content-addressed, not inlined into the ledger.

```python
class ScreenedArtifact(BaseModel):
    artifact_id: str                 # "scr_" + sha256 prefix, 16 hex
    parent_trajectory_id: str | None
    import_hash: str | None          # exactly one of parent or import_hash
    criteria_hash: str               # pre-registered; mismatch is a reject
    task_class: str
    fresh_execution_id: str
    payload_uri: str                 # content-addressed blob
    payload_sha256: str
    payload_bytes: int               # hard cap: 8 MiB uncompressed
    checks_passed: int
    checks_total: int
    strict_success: bool
    leakage_scan_version: str
    producing_job_id: str
    ledger_receipt: str
    created_at: str                  # ISO-8601 UTC
```

Storage rules:

- Payload is gzip-compressed JSON of the public trajectory (actions, observations, verifier report). Cap is 8 MiB uncompressed. Over cap: reject with `reason="payload_too_large"` and keep a negative case that stores only the hash and the reason, not the body.
- Ledger stores the ScreenedArtifact record, not the payload bytes.
- Dedup key is `(parent_trajectory_id or import_hash, criteria_hash, payload_sha256)`.

### 8.2 Hygiene request and result

```python
class HygieneRequest(BaseModel):
    trajectory_id: str | None
    import_payload: dict | None      # existing TrajectoryImport shape
    criteria_hash: str
    task_class: str
    sandbox_policy_id: str
    idempotency_key: str             # caller-supplied; required

class HygieneResult(BaseModel):
    accepted: bool
    reason: Literal[
        "ok",
        "verifier_fail",
        "leakage",
        "timeout",
        "sandbox_error",
        "criteria_mismatch",
        "payload_too_large",
        "duplicate",
    ]
    artifact_id: str | None
    fresh_execution_id: str | None
    ledger_receipt: str
    checks_passed: int
    checks_total: int
    strict_success: bool
    duplicate_of: str | None         # artifact_id when reason="duplicate"
```

### 8.3 Leakage

```python
class LeakageFinding(BaseModel):
    code: Literal[
        "private_guidance",
        "system_prompt_fragment",
        "host_path",
        "secret_env",
    ]
    span_hash: str                   # hash of the offending span, not the span
```

The detector is a pure function. It does not log the offending text. Fixture spans live only in the test suite.

### 8.4 Calibration and scores

```python
class CalibConstants(BaseModel):
    version: str                     # "calib_" + content hash
    family_quantile: dict[str, float]  # 0.90 quantile of check counts
    fallback_d: float = 82.0
    partial_scale: float = 0.6      # must be < 1.0
    approved_by: str                 # human gate record id
    approved_at: str

class ProgressScores(BaseModel):
    constants_version: str
    dense: float
    prioritized: float
    strict_success: bool
```

```python
def family_dense_reward(checks_passed: int, family: str, c: CalibConstants) -> float:
    d_f = c.family_quantile.get(family, c.fallback_d)
    if d_f <= 0:
        return 0.0
    return max(checks_passed / d_f, 0.0)

def success_prioritized_reward(
    strict_success: bool, checks_passed: int, family: str, c: CalibConstants
) -> float:
    d_f = c.family_quantile.get(family, c.fallback_d)
    if strict_success:
        denom = checks_passed + d_f
        partial = (checks_passed / denom) if denom else 0.0
        return 1.0 + partial
    return family_dense_reward(checks_passed, family, c) * c.partial_scale
```

Calibration acceptance (offline, golden corpus):

- Let `failure_share` be the fraction of total prioritized-score mass on runs where `strict_success` is false.
- Accept a constant set only if `failure_share_new <= failure_share_old - 0.15`.
- Strict-success ordering of task classes must be unchanged (Spearman rank of strict-success rates identical).
- `partial_scale` stays below 1.0 so a fully checked failure cannot outrank a strict success.

### 8.5 Step credit

```python
class StepCredit(BaseModel):
    artifact_id: str
    turn_id: str
    credit: float                    # sums to prioritized score
    gamma_turn: float                # T2, default 0.95
    constants_version: str
```

Distribution: group actions by `turn_id` (or skill `step_id` when present). Inside a turn, credit is uniform across decision steps. Across turns, weight at hop `k` is `gamma_turn ** k`, then normalize so credits sum to the prioritized score. Observation-only tokens receive zero credit.

### 8.6 Recalibration manifest

```python
class RecalibrationManifest(BaseModel):
    manifest_id: str
    snapshot_id: str
    candidate_skill_ids: list[str]
    passed: bool
    validator_failures: int
    sample_size: int
    duration_s: float
    ledger_receipt: str
    created_at: str
```

SkillVersion lineage, added fields only:

```python
class SkillLineage(BaseModel):
    screened_artifact_id: str
    screened_payload_sha256: str
    criteria_hash: str
    recalibration_manifest_id: str   # required before active
    parent_skill_id: str | None
```

### 8.7 Stage entry

```python
class StageEntry(BaseModel):
    snapshot_id: str
    stage: Literal["hygiene", "distill", "recalibrate", "promote", "rollback"]
    retained: bool
    metrics: dict[str, float]        # process_score, strict_count, failure_share
    parent_receipt: str
    ledger_receipt: str
```

## 9. Interface catalog

All calls are in-process Python. HTTP wrappers, if added, mirror these contracts and add no extra authority. Auth is the existing operator gate. There is no new network trust boundary.

### 9.1 `hygiene_gate(req: HygieneRequest) -> HygieneResult`

| Item | Contract |
|------|----------|
| Protocol | In-process. Optional `POST /v1/trajectories/hygiene` maps 1:1 onto this function. |
| Idempotency | Required `idempotency_key`. Same key returns the stored result and does not re-execute. |
| Dedup | Same dedup key as §8.1 returns `reason="duplicate"` and `duplicate_of`. |
| Preconditions | `criteria_hash` matches the locked intake hash. Sandbox policy id is on the allow-list. |
| Side effects | One episodic write, one ledger append. No skill write, no index write. |
| Errors | `criteria_mismatch`, `timeout` (sandbox deadline, default 1× the solve budget for that task class), `sandbox_error`, `payload_too_large`. These are results, not exceptions, except for programmer errors. |
| Rate | Bounded by the sandbox pool. Excess requests queue on the existing improvement-plane scheduler. No silent drop. |
| Concurrency | See §9.4. |

Flow:

1. Resolve idempotency key. Return stored result if present.
2. Load locked criteria by hash. Mismatch → `criteria_mismatch`.
3. Acquire the dedup lock (§9.4).
4. Launch a fresh container with the public prefix only.
5. Re-execute. Record verifier outcome against the locked criteria.
6. Run LeakageScreen. Any finding → reject.
7. On dual pass, write ScreenedArtifact and receipt. On fail, write a negative case and receipt.
8. Release the lock. Return HygieneResult.

### 9.2 `screen_leakage(actions, observations, scan_version) -> list[LeakageFinding]`

Pure. No I/O. Version is part of the ScreenedArtifact so a scan-rule change does not silently rewrite history.

### 9.3 `run_recalibration(snapshot_id: str) -> RecalibrationManifest`

| Item | Contract |
|------|----------|
| Idempotency | Keyed by `(snapshot_id, candidate skill id set hash)`. A second call returns the existing manifest. |
| What it runs | Existing golden validators plus a stratified sample of recent episodic cases. Retrieval is enabled. No new solves. |
| Pass rule | Zero validator failures on the golden set. Sample failures are recorded and do not by themselves fail the pass unless they hit a pre-registered must-pass criterion. |
| Duration budget | `< 5%` of the wall-clock of a full practice job for the same snapshot. Exceeding the budget fails the pass with a duration breach; it does not skip the pass. |
| Side effects | One manifest, one ledger append. Does not rebuild the index. |
| Blocking | Promotion of this snapshot waits on `passed=True`. |

### 9.4 Concurrent hygiene

Lock key: `(parent_trajectory_id or import_hash, criteria_hash)`.

- Lock is held only for the accept-or-reject write, not for the whole sandbox run. The sandbox run uses a single-flight token on the same key so two callers do not double-execute.
- Loser of the single-flight waits and then returns `duplicate` or the winner’s result.
- Crash before the ledger append leaves no artifact. The next caller re-executes. That is safe because re-execution is deterministic with respect to acceptance, not with respect to timing.
- Crash after the ledger append is recovered by the idempotency key.

### 9.5 Index commit (two-phase)

```text
phase prepare:
  build next index as an inactive generation G+1
  record IndexPrepared(snapshot_id, generation, manifest_id) on the ledger
  do not move the live pointer

phase commit:
  require RecalibrationManifest.passed for that snapshot
  flip the live pointer from G to G+1 in one ledger append: IndexCommitted
  readers always load the pointer, then the generation it names

phase abort:
  IndexAborted; G stays live; G+1 is eligible for deletion
```

If the process dies after prepare and before commit, restart sees `IndexPrepared` without `IndexCommitted` and either completes the flip (manifest still passed) or aborts. Readers never observe a mix of generations.

### 9.6 Retrieval contract assertion

The execution-plane retrieve function returns SkillVersions whose lineage has a `recalibration_manifest_id` and whose id is in the live index generation. A test fixture that inserts a ScreenedArtifact and then calls retrieve must get zero hits for that artifact. This test is a release blocker.

## 10. Memory management

Versioning:

- No in-place edit of a skill, a constant set, or a manifest.
- Distill creates a new SkillVersion whose lineage points at the ScreenedArtifact.
- Revert is a ledger-head restore: drop the new SkillVersion, drop its manifest from the eligible set, and point the index at the prior generation.

Active set:

- ScreenedArtifacts do not count toward the active cap.
- Negative cases do not count toward the active cap.
- Retirement continues to use measured contribution. StepCredit and prioritized score are inputs to that ranking. They are not a second retirement policy.
- During the dual-score soak, retirement decisions keep using the old score. The new score is written beside it and is not allowed to retire anything until Phase 4.

Replay:

A ledger head reconstructs:

- the set of accepted ScreenedArtifacts,
- the CalibConstants version in force,
- the RecalibrationManifest that authorized the snapshot,
- the StepCredit rows used for reporting,
- the live index generation.

Historical lift is recomputed from raw check results, not from prioritized scores, so a later constant change cannot rewrite a past lift number.

Retention:

- Undistilled ScreenedArtifacts follow existing episodic retention.
- StepCredit and StageEntry are kept for the soak window plus one re-certification cycle, then archived.
- Ledger receipts are not auto-deleted.

Integrity tests added in Phase 0:

- Improvement-plane modules cannot import the T3 config module.
- CalibConstants writes fail unless the caller is the human-gate path.
- Retrieve never returns a ScreenedArtifact id.
- A criteria hash mismatch never produces an accepted artifact.

## 11. Scoring overlay and measurement

Raw check results stay immutable. The calibrator writes a sidecar score keyed by run id and constants version. Reports that claim lift keep using the control arm. Reports that rank contribution may use the prioritized score only after Phase 4.

Operator-visible pair, per snapshot:

| View | Definition |
|------|------------|
| Process score | Mean prioritized score across the snapshot’s evaluated runs |
| Strict completion | Count of runs with `strict_success` |
| Failure share | Share of prioritized-score mass on non-success runs |
| Control-arm lift | Unchanged existing definition |

A library change that moves process score but not control-arm lift is not an improvement. The console must show both.

## 12. Capacity gate

Phase 0 measures, on a representative distill batch (or a replay of the last 30 days of distill candidates, whichever is smaller but at least 50 trajectories):

- `reexec_fraction`: share of candidates that would enter HygieneGate.
- `median_reexec_s` and `p95_reexec_s`.
- `pool_headroom`: free sandbox slots divided by slots required to drain one batch without exceeding the current distill SLO.

Phase 1 may start only if `pool_headroom >= 1.5` at p95, or a written operator decision accepts a longer distill SLO and records the new SLO. Sampling of low-value task classes (S2) is forbidden until this measurement exists. It is not a way to skip the measurement.

## 13. Rollback runbook

Trigger: any measured drop in control-arm first-attempt success after Phase 3, on the pre-registered soak set, outside the pre-registered noise band. This is a release blocker. It is not a tuning opportunity.

Steps:

1. Set `RECERTIA_HYGIENE_ENABLED=0` and `RECERTIA_CALIBRATED_SCORING=0`.
2. Read the ledger for the last `stage="promote"` entry whose snapshot was in the pre-change soak baseline. That entry’s `parent_receipt` is the restore head `H`.
3. Append `StageEntry(stage="rollback", retained=False)` pointing at the current head.
4. Rebuild is not a rebuild from scratch. Call index abort-or-restore so the live pointer names the generation recorded at `H`.
5. Verify: active skill id set equals the set recorded in the StageEntry at `H`. Retrieve of a post-change skill id returns empty. A control-arm smoke sample matches the baseline within the noise band.
6. Leave new episodic artifacts in place. They are inert while the flags are off and the index points at `H`.
7. Open a ledger-linked incident. Do not delete receipts to “clean up” the failure.

Flags default to off through Phase 3. Phase 4 removes them only after the soak passes.

## 14. ADRs

### ADR-001 — Trajectory hygiene gate before distill (2026-11-08)

Status: Accepted for this spec.  
Decision: Every trajectory-derived candidate passes fresh re-execution and leakage screening before a skill candidate exists.  
Rationale: Originating-run verification is not reproducible acceptance.  
Consequences: Extra sandbox cost, bounded by the capacity gate. Rejects become negative knowledge.

### ADR-002 — Family-normalized and success-prioritized scoring (2026-11-08)

Status: Accepted for this spec.  
Decision: Contribution and, after soak, retirement inputs use calibrated scores. Promotion stays strict success plus control-arm lift.  
Rationale: Stops partial-progress inflation without moving the honesty gate.  
Consequences: Dual-write during soak. Old scores remain the retirement authority until Phase 4.

### ADR-003 — Mandatory post-update recalibration (2026-11-08)

Status: Accepted for this spec.  
Decision: A snapshot is retrieval-eligible only after a passed RecalibrationManifest, and the index swaps by two-phase commit.  
Rationale: Same job as a critic warm-up, without a value network: do not let stale validators affect high-stakes retrieval.  
Consequences: Promotion latency grows by the recalibration budget.

### ADR-004 — Screened artifacts are not skills (2026-11-08)

Status: Accepted for this spec.  
Decision: ScreenedArtifact lives in the episodic store. Retrieve cannot see it.  
Rationale: Keeps the active set equal to re-certified SkillVersions.  
Consequences: Distill is a required hop. That is intentional.

## 15. Roadmap

### Phase 0 — Specification, fixtures, and capacity baseline

Deliver:

- This document, treated as the contract.
- Leakage fixture suite, at least 20 reject cases covering the four finding codes.
- Known-clean fixture suite, at least 20 accept cases, including verbose logs that are not leakage.
- Partial-but-not-strict fixture suite for the calibrator.
- `CalibConstants` schema and the offline calibration script, run once to produce a candidate constant set. Do not activate it.
- Capacity-gate measurement (§12).
- Import-boundary tests and the retrieve-exclusion test, failing until the code exists, checked in as expected failures that Phase 1 flips.
- Rollback runbook dry-run against a fixture ledger.

Exit: fixtures green, capacity number written down, ADRs unchanged, no production path change.

### Phase 1 — HygieneGate

Deliver:

- `hygiene_gate`, `screen_leakage`, episodic writes, ledger receipts, idempotency, single-flight dedup.
- Distill and import call the gate when `RECERTIA_HYGIENE_ENABLED=1`. Default remains 0 until the phase exit is signed.
- Metrics: `hygiene_reject_rate` by reason, `hygiene_reexec_s` p50/p95.

Exit:

- 100% rejection on the leakage suite.
- 100% acceptance on the known-clean suite.
- Every accept has a receipt and a `fresh_execution_id`.
- Wall-clock delta versus the capacity-gate prediction is recorded.
- False-positive rate on the known-clean suite is zero. Borderline production rejects during soak go to the human queue (S3) and do not auto-promote.

### Phase 2 — Scoring overlay and attribution

Deliver:

- ProgressCalibrator, StepBoundaryAttributor, sidecar scores, dual-write.
- Console shows old and new scores.
- Retirement still reads the old score.

Exit:

- Failure-share reduction of at least 15 percentage points on the golden corpus.
- Strict-success rank order unchanged.
- Promotion gate unchanged.

Soak for the dual-score period: 14 days or 30 promotion decisions, whichever comes later. Abort the soak if any old-score retirement decision would have flipped under the new score on a skill with contribution above the current retirement threshold. That flip is a finding, not an authorization to switch.

### Phase 3 — Recalibration and stage ledger

Deliver:

- `run_recalibration`, two-phase index commit, StageLedger, lineage field for `recalibration_manifest_id`.
- Promotion refuses a snapshot with no passed manifest.
- Console shows retained versus discarded and process versus strict.

Exit:

- A snapshot cannot be retrieved until commit.
- Killing the process between prepare and commit leaves the prior generation live (fault-injection test).
- Control-arm first-attempt success on the soak set is inside the pre-registered noise band.
- Recalibration wall-clock is under 5% of the matching practice job.

### Phase 4 — Default on

Deliver:

- Flags default on, then the legacy branches are deleted in a follow-up change only after one more soak cycle.
- Operator runbook updated.
- Architecture docs updated to point at this spec.

Exit: full soak passes, no control-arm regression, legacy path deleted, rollback runbook still works against the last pre-flag-removal head.

## 16. Operations

Jobs run on the existing improvement-plane scheduler and the existing sandbox. No new runtime and no new privilege.

Metrics:

| Metric | Alert |
|--------|-------|
| `hygiene_reject_rate{reason}` | Leakage rate above the soak baseline by 2× for 24h |
| `hygiene_reexec_s` p95 | Above the capacity-gate p95 by 50% |
| `failure_score_share` | Rises after Phase 4 |
| `recalibration_duration_s` | Above 5% of practice-job wall-clock |
| `stage_retained_count` | No alert; operator surface |
| `index_generation_skew` | Live pointer not equal to last IndexCommitted |

Rollback is §13. Knowledge transfer is the runbook plus this spec. On-call load stays on the existing soak failure channel.

## 17. Risk

| Risk | Containment |
|------|-------------|
| Distill cost inflation | Capacity gate before Phase 1. Sampling only after measurement, and only as S2. |
| Retirement ranking flips | Dual-write. Old score stays authoritative through Phase 3. Soak abort rule in Phase 2. |
| Index half-commit | Two-phase pointer. Fault-injection test is a Phase 3 exit. |
| False-positive leakage | Known-clean suite is a 100% accept gate. Human queue cannot promote. |
| Stale validators after a batch | RecalibrationPass is blocking. |
| Honesty drift | Control-arm regression is an automatic rollback. Calibrated scores cannot promote. |
| T3 leak or weight update | Out of scope and enforced by import-boundary tests. A review that proposes either is a defect in the change, not a follow-up idea. |

## 18. Work packages

| Package | Owner role | Done when |
|---------|------------|-----------|
| Fixtures and calibration script | Measurement | Phase 0 exit |
| Capacity baseline | Operations | Number written, go/no-go recorded |
| HygieneGate + leakage + dedup | Improvement plane | Phase 1 exit tests green |
| Calibrator + attributor + sidecar | Measurement | Phase 2 exit |
| Recalibration + index commit + lineage | Memory | Phase 3 fault-injection test green |
| Console views | Operator surface | Process vs strict and retained vs discarded visible |
| Rollback dry-run | Operations | §13 steps executed on a fixture ledger |
| Import-boundary and retrieve-exclusion tests | Integrity | CI release blockers |

## 19. Decision required next

Accept this document as the Phase 0 contract, or mark the specific section that is wrong. Phase 1 coding does not start until the capacity-gate number exists and the leakage plus known-clean fixture suites are green.

No production flag flips in Phase 0.
