# Recertia 2030 Technical Workplan — Hardened
## One Large Sprint: Measurement Continuity & Operator Foundation (Prioritized Items 1–30)

**Status:** Final (hardened after it-architect double-check). Detailed specification files created.  
**Date:** 2026-10-26  
**Scope:** Plan and specifications only. No implementation code.  
**Branch:** `docs/2030-sprint-workplan-hardened`  
**Soak status:** Host powered off; no local server access. All deliverables offline-capable.

---

## 1. Vision (target state)

Operators can continue rigorous measurement, week-close style review, and honest reporting of implicit recovery and causal lift even while the soak host is powered off. The system maintains a non-modifiable measurement plane, clear plane boundaries, and library-as-portfolio visibility. Future work compounds on libraries, judges, and measurement integrity rather than graph topology. Evidence remains the gate; negative or unestablished results halt expansion.

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
- Honest metrics: every new surface carries unavailable reasons and “not established” labeling when intervals span zero or data are sparse. No silent zeros.
- No writes from UI, offline packets, annotations, or clients into RunState, MemoryBundle, or CONSTRAINTS.md.
- Additive schemas only. Characterization tests freeze existing `week_close`, `monday_close`, `lift_label`, and `stop_plan` behavior.
- Second domain (including trading) must absorb with zero structural graph change.
- All clients (Grok-Bot, GROVE, etc.) remain off-graph.

Every new specification MUST contain a short “T3 / non-modifiable surfaces” subsection and a characterization-test requirement.

## 4. Dependency Map

1. Measurement Continuity Kit + characterization tests (hard prerequisite; enables everything else offline).
2. Honest Interval & Assumption Dashboard (consumes packets).
3. Operator OS boundaries and annotation store (read-only on existing data).
4. Library-as-Portfolio and Portable Goal Certificate extensions (reuse existing metrics).
5. Physics-inspired measurement pipeline (operates on packets).
6. Anti-corruption layers and multi-system contracts.
7. Remaining medium-priority items.

Continuity Kit is the critical path. Nothing else proceeds without frozen characterization fixtures.

## 5. Workstreams and New Specifications

### WS1 — Measurement Continuity Kit (Items 1, 6)
**File:** `docs/specifications/measurement-continuity-kit.md` (created)

### WS2 — Operator OS & Honest Interval Dashboard (Items 2, 3, 9, 12)
**File:** `docs/specifications/operator-measurement-continuity.md` (created)

### WS3 — Library-as-Portfolio & Portable Goal Certificate (Items 4, 5, 11)
**Files:** `docs/specifications/library-portfolio.md`, `docs/specifications/goal-certificate-portable.md` (created)

### WS4 — Physics-Inspired Measurement Pipeline (Item 7)
**File:** `docs/specifications/physics-measurement-pipeline.md` (created)

### WS5 — Anti-Corruption Layers & Non-Modifiable Measurement Plane (Items 8, 10, 16)
**File:** `docs/architecture/anti-corruption-layers.md` (created)

### WS6 — Remaining medium-priority items
Light notes folded into the above. All subject to the same invariants.

## 6. Sequencing (documentation only)

Critical path (Continuity Kit) and primary specifications completed. Remaining: characterization fixture details (when host or recorded packets available) and human review.

## 7. Acceptance Criteria (architectural quality gates)

- All new specs reference existing contracts (MetricReport, Goal Certificate, week_close, measurement-and-scope.md, ADR-0005). **Met.**
- Characterization tests defined that freeze current soak-tree week-close behavior. **Outlined; fixtures pending host.**
- Data ownership table present for every new artifact. **Met.**
- Explicit statement that no UI, packet, or annotation has a write path into RunState or T3 surfaces. **Met.**
- Offline packets produce valid “not established” reports. **Specified.**
- Control-arm identity invariant stated and testable. **Met.**
- Model-id isolation (solver vs verifier) check included. **Met.**
- Non-goals restated and enforced. **Met.**

## 8. Risks and Mitigations

- Spec drift from existing contracts → cross-references + characterization tests.
- Accidental RunState write paths → data ownership table + explicit “no import” language.
- Over-scoping into implementation → strict “plan and specs only” rule.
- Counting empty weeks → explicit non-counting rule in packet schema.
- Tree divergence confusion → current-state assessment restated.

## 9. Next Actions (quality-gated)

1. Human reviews the detailed specifications on the branch.
2. Characterization fixtures created from the current soak tree (when host is available or from recorded packets).
3. No merge to main until human [F] and existing CI gates pass.

## 10. Non-Goals (reaffirmed)

- No 16th node or graph growth.
- No self-modification of measurers or constraints.
- No unparking of Evidence-Bound Transfer or full D2 literature inbox in this sprint.
- No auto-promotion or auto-retirement.
- No dual-write of fills.
- No live host traffic required for these specifications.
- No claiming of RW-GA or `ga_claimed = true`.

---

**End of hardened workplan.** Detailed specifications created on this branch. Consistent with standing Recertia constraints and the it-architect double-check. Ready for human review.
