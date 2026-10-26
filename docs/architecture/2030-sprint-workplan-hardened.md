# Recertia 2030 Technical Workplan — Hardened
## One Large Sprint: Measurement Continuity & Operator Foundation (Prioritized Items 1–30)

**Status:** Updated after harder re-synthesis (2026-10-26). Detailed specification files aligned.  
**Date:** 2026-10-26  
**Scope:** Plan and specifications only. No implementation code.  
**Branch:** `docs/2030-sprint-workplan-hardened`  
**Soak status:** Host powered off; no local server access. All deliverables offline-capable.

---

## 1. Vision (target state)

Operators can continue rigorous measurement, week-close style review, and honest reporting of implicit recovery and causal lift even while the soak host is powered off. The system maintains a non-modifiable measurement plane, clear plane boundaries, and library-as-portfolio visibility. Future work compounds on libraries, judges, and measurement integrity rather than graph topology. Evidence remains the gate; negative or unestablished results halt expansion. “Not established” is the expected default state while the host is off.

## 2. Current-State Assessment

- Soak host is powered off. Counted RW-GA weeks are open; `ga_claimed` remains false.
- Public `recertia/recertia` main has advanced on reliability (store receipts P1, multi-tenant ADR-0021 phases) but does not contain local OC0–OC4 or Goal Transfer (ADR-0016 numbering collision exists).
- Local soak tree holds the running implementation. Tree divergence is acknowledged.
- Measurement integrity, control arm, and honest unavailable paths are already designed (measurement-and-scope.md, measurement-integrity.md, ADR-0005).
- Horizon note (loops-and-graphs-horizon.md) is locked: focus on libraries, judges, and measurement; not topology growth.

## 3. Architectural Invariants (mandatory)

These bind every specification in this sprint:

- **ADR-0001**: Graph with loops; no growth. No 16th node. No new edges that collapse planes.
- **ADR-0002**: Plural memory. Retrieval remains mandatory and separate.
- **ADR-0005 / T3 boundary**: The system may not modify the mechanisms that measure or constrain it. Measurement, judge isolation, promotion thresholds, ablation rate, and graph topology remain T3 (human-authored only).
- Control-arm identity: treatment and control differ only by the deliberate ablation (memory on/off).
- Honest metrics: every new surface carries unavailable reasons and “not established” labeling when intervals span zero or data are sparse. No silent zeros. “Not established” is the default design state while the host is off.
- No writes from UI, offline packets, annotations, or clients into RunState, MemoryBundle, or CONSTRAINTS.md.
- Additive schemas only. Characterization tests freeze existing `week_close`, `monday_close`, `lift_label`, and `stop_plan` behavior.
- Second domain (including trading) must absorb with zero structural graph change.
- All clients (Grok-Bot, GROVE, etc.) remain off-graph.
- Explicit stop conditions: negative or unestablished a1, failed characterization tests, control-arm divergence halt expansion.

Every new specification MUST contain a short “T3 / non-modifiable surfaces” subsection and a characterization-test requirement.

## 4. Revised Prioritization (harder re-synthesis)

**Tier A — Measurement substrate (must exist and be frozen first; fully offline-capable)**  
1. Measurement Continuity Kit + characterization fixtures (empty week, silent-zero trap, recorded packet) that lock week_close, stop_plan, and lift_label behavior, including schema version and eval-firewall invariant.  
2. Honest interval surfaces with “not established” as the default design state (Wilson intervals, control-arm identity fixture, floor-pressure metrics — library_yield, active_cap_pressure, retirement_reversal_rate — as primary).  
3. Continuous judge-isolation auditor (solver ≠ verifier model ids) with offline fixture that fails on match.  
4. Non-modifiable measurement plane + living non-goals register with explicit stop conditions.  
5. Pre-registration / analysis freeze practice for any future claim about causal_lift or a1.

**Tier B — Outer-loop compounding (only after Tier A fixtures exist)**  
6. Physics-inspired measurement pipeline (Monte Carlo closure within 2 SE in ≥95% of 100 runs, explicit power formula with design-effect, blinded protocol).  
7. Portable Goal Certificate (additive schema, separate checker preserved, pre-existing certificates still validate).  
8. Library-as-Portfolio with derived risk-adjusted views that are display-only (never stored in MetricReport, never used as promotion or retirement input).  
9. Ask-Early value-decay treated as a measured assumption (honest interval or unavailable; literature value collapses after ~10% of trajectory).  
10. Episodic dead-end / negative knowledge retrieval quality telemetry (pure reader).  
11. External annotation store (pure reader, zero import path into RunState or MemoryBundle).

**Tier C — Strictly gated by evidence and zero structural change**  
12. Trading / second domain as a parallel portfolio scored exclusively by existing Recertia metrics (parameter-surface exploration is offline proposal only; no new task classes or graph edges).  
13. Multi-system command center with anti-corruption labels on every external system (pure reader of existing planes).

## 5. Dependency Map

1. Measurement Continuity Kit + characterization fixtures (hard prerequisite; primary durable offline artifact).  
2. Honest Interval & floor-pressure surfaces (consume packets; “not established” default).  
3. Continuous judge-isolation auditor and non-modifiable plane.  
4. Pre-registration practice and physics pipeline (after fixtures).  
5. Outer-loop compounding (Goal Certificate, library portfolio display-only, dead-end retrieval, annotation).  
6. Gated extensions (trading, multi-system).

Continuity Kit and characterization fixtures are the critical path. Nothing else proceeds without them.

## 6. Workstreams and New Specifications

### WS1 — Measurement Continuity Kit (Tier A)
**File:** `docs/specifications/measurement-continuity-kit.md` (created and hardened)

### WS2 — Operator OS & Honest Interval Dashboard (Tier A/B)
**File:** `docs/specifications/operator-measurement-continuity.md` (updated for floor-pressure primary and not-established default)

### WS3 — Library-as-Portfolio & Portable Goal Certificate (Tier B)
**Files:** `docs/specifications/library-portfolio.md`, `docs/specifications/goal-certificate-portable.md` (created)

### WS4 — Physics-Inspired Measurement Pipeline (Tier B)
**File:** `docs/specifications/physics-measurement-pipeline.md` (created and hardened)

### WS5 — Anti-Corruption Layers & Non-Modifiable Measurement Plane (Tier A)
**File:** `docs/architecture/anti-corruption-layers.md` (created)

### WS6 — Remaining medium-priority items
Light notes folded into the above. All subject to the same invariants and Tier ranking.

## 7. Sequencing (documentation only)

Critical path (Tier A Continuity Kit and characterization fixtures) completed and hardened. Remaining: actual characterization fixtures (when host or recorded packets available) and human review. Tier B/C remain gated.

## 8. Acceptance Criteria (architectural quality gates)

- All new specs reference existing contracts (MetricReport, Goal Certificate, week_close, measurement-and-scope.md, ADR-0005). **Met.**
- Characterization tests defined that freeze current soak-tree week-close behavior. **Outlined with concrete fixtures; actual fixtures pending host.**
- Data ownership table present for every new artifact. **Met.**
- Explicit statement that no UI, packet, or annotation has a write path into RunState or T3 surfaces. **Met.**
- Offline packets produce valid “not established” reports. **Specified.**
- Control-arm identity invariant stated and testable. **Met.**
- Model-id isolation (solver vs verifier) check included. **Met.**
- Non-goals restated and enforced. **Met.**
- Floor-pressure metrics elevated to primary. **Met in operator spec.**
- “Not established” treated as default design state. **Met.**

## 9. Risks and Mitigations

- Spec drift from existing contracts → cross-references + characterization tests.
- Accidental RunState write paths → data ownership table + explicit “no import” language.
- Over-scoping into implementation → strict “plan and specs only” rule.
- Counting empty weeks → explicit non-counting rule in packet schema.
- Tree divergence confusion → current-state assessment restated.
- Premature expansion before fixtures → Tier ranking and stop conditions.

## 10. Verification Checklist (updated for harder synthesis)

- [ ] Every new spec contains a T3 / non-modifiable surfaces subsection.
- [ ] Data ownership table present and no RunState write path exists.
- [ ] Characterization test outline includes silent-zero trap fixture.
- [ ] Control-arm divergence fixture defined.
- [ ] Floor-pressure metrics are primary surfaces.
- [ ] “Not established” is the default design state.
- [ ] Annotation store has explicit no-import rule.
- [ ] Risk-adjusted lift is display-only.
- [ ] POST /v1/runs fail-closed language present.
- [ ] Old Goal Certificates still validate under additive schema.
- [ ] Power formula and closure acceptance criteria present.
- [ ] Offline packets set counts_toward_week: false.
- [ ] Explicit stop conditions listed.

## 11. Next Actions (quality-gated)

1. Human reviews the detailed specifications on the branch.
2. Characterization fixtures created from the current soak tree (when host is available or from recorded packets).
3. No merge to main until human [F] and existing CI gates pass.

## 12. Non-Goals (reaffirmed)

- No 16th node or graph growth.
- No self-modification of measurers or constraints.
- No unparking of Evidence-Bound Transfer or full D2 literature inbox in this sprint.
- No auto-promotion or auto-retirement.
- No dual-write of fills.
- No live host traffic required for these specifications.
- No claiming of RW-GA or `ga_claimed = true`.
- No claims of support from sparse or fixture data.

---

**End of updated hardened workplan.** Aligned with the harder re-synthesis. Detailed specifications updated on this branch. Consistent with standing Recertia constraints. Ready for human review.
