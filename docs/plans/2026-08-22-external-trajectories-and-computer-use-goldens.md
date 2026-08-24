# External Trajectories & Computer-Use Goldens — Implementation Plan

**Date:** 2026-08-22  
**Status:** Phase 0 landed. ADR-0019 rewrite (2026-08-24) lands distill-candidate, HTTP import, pending proposal, gated `external_computer`, JobQuota share, operator brief. Promotion-with-lift and a live computer backend remain open.  
**Sources:**  
- Architect review of filtered Grok Bot use-case patterns (teach-once, bug-repro, playtest, docs-auditor, practice density, operator briefs)  
- Technical implementation plan & contracts (this document)  
- Existing invariants: retrieve-before-invent, control-arm lift, plane separation, `--rm` isolation, bounded library  

**Scope:** Absorb high-value computer-use patterns as trajectories and golden task classes while Recertia remains the sole measuring orchestrator. No new graph nodes, no Bot fleet, no persistent-VM default, no consumer/GTM surfaces.

---

## Executive Summary

| # | Item | Landing surface | Status |
|---|------|-----------------|--------|
| 1 | Teach-once trajectory → promotion-gated skill | Episodic → Distill → Review → Procedural | Import + distill-candidate landed; promotion still requires review + control-arm lift |
| 2 | Bug Reproduction / Playtest Operator / Docs Auditor goldens | Task-class registry + evals | Phase 0: skill-free fixtures under `evals/golden/` (snake_case). Not on the repo-chore gate |
| 3 | Writes/spend behind approval | Already present (review node) | No change |
| 4 | Practice density under JobQuota (no 16th node) | Improvement plane | `computer_use_practice_share` (0.15) on JobQuota; snake task classes |
| 5 | Operator brief (stuck / lift / redundancy) | Systems / Tower projection | `recertia systems --brief`; computer-use lift language is "not established" |

Recertia stays the measuring orchestrator. External agents supply trajectories and specialized executors; only Recertia decides what enters durable memory and whether it still works.

---

## Goals

1. Import external trajectories (recordings, solved runs) with full provenance into the Episodic store.
2. Distill them under existing quality gates; promote only after positive control-arm lift.
3. Seed three computer-use golden task classes expressible in the existing criteria language.
4. Keep long-lived computers optional, allow-listed, and never the default security boundary.
5. Surface stuck jobs, lift deltas, and redundancy in the existing Systems view without inventing a Chief-of-Staff persona.

All changes remain scaffolding-only. No weight updates. No self-edit of the referee. Graph topology stays at the current ~15-node single-agent default.

---

## Non-Goals (explicit)

- Standing named teammates or multi-Bot topology  
- Persistent VMs as default execution backend or security boundary  
- Auto-write of approved state  
- Consumer/GTM surfaces (sales, inbox, personal chores, etc.)  
- Graph growth or new planes  
- Any relaxation of retrieve-before-invent, criteria locking, control-arm lift, or plane separation  

---

## Architectural Constraints (must preserve)

- Planes: Execution (bounded, per-request), Memory (durable, versioned, attributable), Improvement (offline, scheduled, quality-gated).  
- Default execution: `--rm` containers, network-none, nobody user, per-attempt workdirs.  
- Promotion requires automatic validation + human review gate + positive control-arm lift.  
- Every artifact attributable and revertible.  
- Library remains bounded; weak skills retire on measured contribution.  
- Failures retained as first-class episodic knowledge.  

---

## Contracts (new / extended)

### TrajectoryImport

```text
TrajectoryImport
  import_id: ULID
  source: "grok_bot_recording" | "grok_bot_run" | "external_demo" | "synthetic"
  source_ref: str
  captured_at: datetime
  environment: EnvironmentDescriptor   # OS, browser, tools, versions, network policy
  steps: list[TrajectoryStep]
  outcome: "solved" | "failed" | "partial"
  criteria_snapshot: list[TaskCriterion]
  provenance: ProvenanceBundle
  artifacts: list[ArtifactRef]         # screenshots, HAR, terminal logs
  reexecutable: bool
```

**Import rules**  
- Reject incomplete provenance or missing environment descriptor.  
- If `reexecutable=False`, trajectory may enter Episodic only; cannot promote until a re-validation path exists.  
- Import is append-only.

### Golden Task Classes

| Task Class | Primary Criteria Shape | Required Artifacts |
|------------|------------------------|--------------------|
| `bug_reproduction` | command + file/screenshot hash + network notes | steps, screenshots, HAR, terminal log |
| `playtest_operator` | UI assertion sequence + final state predicate | step log, screenshots, final state snapshot |
| `docs_auditor` | product-vs-docs diff + non-regression assertions | before/after diffs, missing-page list |

All three must support clean memory-off control runs and independent fixtures.

### Affordance / Tool Registry

Optional executor kind `external_computer` (backend, allowlist policy, session TTL, isolation mode). Default remains container. Long-lived requires explicit policy flag and is never the security boundary.

---

## Phased Implementation

### Phase 0 – Foundation (contracts + goldens) — **landed**
- [x] Author `TrajectoryImport` contract and provenance schema.  
- [x] Define the three golden task-class contracts and minimal synthetic fixtures.  
- [x] CLI: `recertia trajectory import <path>` with strict validation.  
**Exit:** Import rejects incomplete provenance; golden suite runs under existing lift harness (`recertia eval run --task-class {bug_reproduction,playtest_operator,docs_auditor}`); fixtures are eval-firewalled and not on the repo-chore promotion gate; no isolation regressions on default path. Distill and promotion remain Phase 1.

### Phase 1 – Distill Path — **candidate path landed; promotion still open**
- [x] TrajectoryImport → Episodic → Distill **candidate** (`recertia trajectory distill --task-class`). Never writes approved. Refuses `true`-noop steps/criteria.  
- [x] Pending proposal when `import_may_promote` (reexecutable + auditor re-verify + criteria snapshot).  
- [ ] Review → control-arm → `promote_to_approved` still uses the existing Improvement-plane flow. No bypass.  
**Exit:** At least one skill promoted with positive measured lift (Wilson interval excludes zero); library bound and retirement still enforced; attribution/lineage intact.

### Phase 2 – Operator Surface + Optional Backend — **gate landed; live backend not wired**
- [x] Systems/Tower projections: stuck jobs, lift-by-class ("not established"), redundancy (`recertia systems --brief`).  
- [x] Optional `external_computer` tool registered (`side_effect=external`). Handler refuses unless `isolation.allow_external_computer`, `isolation.long_lived_computer_backend`, and a non-empty allow-list; even then no standing VM.  
- [x] JobQuota `computer_use_practice_share`.  
**Exit remaining:** a live allow-listed computer is still not a Recertia security boundary and is not wired in this build.

### Phase 3 – Hardening & Cost Control
- Aggressive retirement for computer-use skills.  
- Cost accounting and JobQuota tuning.  
- Shadow trials and evidence-floor enforcement.  
**Exit:** Performance floor (no-memory baseline) holds under library growth; expensive goldens do not starve ordinary practice.

**Rollback triggers (any phase):** promotion that cannot be reversed; control-arm that cannot be executed; relaxation of default isolation; product surface that looks like a standing Bot.

---

## Configuration

There is **no** `improvement.external_trajectory_import` flag. Import is gated by
the CLI / `POST /v1/trajectories/import` and the shipped `TrajectoryImport`
contract. `ImprovementFlags.mea_enabled` stays false.

Shipped on `Policy` / `policy/default.json` with ADR-0018/0019:

```json
{
  "job_quota": {
    "computer_use_practice_share": 0.15
  },
  "state_management": {
    "idle_offload_enabled": false
  },
  "isolation": {
    "default_backend": "container",
    "allow_external_computer": false,
    "long_lived_computer_backend": false,
    "external_computer_ttl_seconds": 3600,
    "external_computer_allowlist": []
  }
}
```

Not shipped, and not planned: `improvement.external_trajectory_import`.

---

## Measurement Integrity

- Every promoted skill distilled from an external trajectory must support a clean memory-off control run inside Recertia’s execution plane.  
- `causal_lift` reported with Wilson interval; “not established” when interval includes zero.  
- Eval firewall remains: golden fixtures never created from runs that produce stored skills.  
- Expensive computer-use trials budgeted separately; may use shadow trials until sample size is adequate.

---

## ADR

[ADR-0019](../adr/0019-external-trajectories.md) (rewrite 2026-08-24 against the
shipped import contract). Companion systems residency: [ADR-0018](../adr/0018-idle-state-offloading.md).


**Title:** External Trajectories and Optional Computer-Use Backends  
**Decision:** Trajectories enter only via `TrajectoryImport` with full provenance; promotion requires Recertia-side validation + control-arm lift; long-lived computer is optional Affordance only, never default security boundary.  
**Consequences:** Realistic UI goldens and teach-once skills under the existing honesty layer; additional cost for control arms; import validation complexity.

---

## Success Metrics

- Positive lift established for at least one computer-use skill class.  
- Zero isolation regressions on default path.  
- Operator can identify stuck jobs and lift deltas from Systems view.  
- Library size and performance floor remain within existing bounds.  
- All external material remains attributable and revertible.

---

## Relation to Remaining Work

This plan does **not** unblock RW-GA, RW-M2, or RW-A. It is a parallel design track that expands the skill-acquisition surface for computer-use domains once operator GA and measurement cadence are solid. It must not delay soak weeks or probe cadence.
