# Recertia 2030 Technical Workplan — Hardened
## One Large Sprint: Measurement Continuity & Operator Foundation (Prioritized Items 1–30)

**Status:** Final (hardened after it-architect double-check)  
**Date:** 2026-10-26  
**Scope:** Plan and specifications only. No implementation code.  
**Branch:** `docs/2030-sprint-workplan-hardened` (created; push timing under human control)  
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
**File:** `docs/specifications/measurement-continuity-kit.md` (to be created in later implementation; outline here)

- Offline week packet format (JSON/YAML) with `offline: true`, source label (fixture/recorded/synthetic), and explicit non-counting rules for empty/fixture weeks.
- Characterization test suite that locks current soak-tree `week_close`, `monday_close`, `lift_label`, and stop_plan conditions.
- Tabletop restore log template (date, run_id, restore_source, time_to_recover, follow-up).
- Honest-interval report generator that labels “not established” correctly.

**Data ownership:** Offline packets are operator tooling. Single writer (human or offline job). Not consultable by retrieve. Not part of the ablation arm. Not written into RunState.

**T3 surfaces:** Counting rules, stop_plan conditions, and week-close checker logic remain non-modifiable.

### WS2 — Operator OS & Honest Interval Dashboard (Items 2, 3, 9, 12)
**File:** `docs/specifications/operator-measurement-continuity.md`

- Surfaces: Wilson intervals for implicit recovery and causal_lift; control-arm delta; explicit “not established” / “unavailable” badges; a1/a2/a4 status panel with evidence links.
- Three-plane timeline (execution / memory / improvement) with causal overlays only where MetricReport provides them.
- Ask-Early indicator (% of trajectory before clarification; advisory only).
- Offline banner when data comes from packets.
- External annotation store (run_id, checkpoint, operator_id, timestamp, note). Read-only on RunState. Zero write path into RunState, MemoryBundle, or constraints.
- Progressive disclosure (summary → intervals → raw).

**Data ownership:** Dashboard is a pure reader of MetricReport and week packets. Annotation store is separate operator tooling. No “apply” or “promote” actions.

**T3 surfaces:** No UI path can alter locked criteria, ablation rate, or measurement code.

### WS3 — Library-as-Portfolio & Portable Goal Certificate (Items 4, 5, 11)
**Files:** `docs/specifications/library-portfolio.md`, `docs/specifications/goal-certificate-portable.md`

- Reuse existing metrics only: library_yield, skill_contribution, active_cap_pressure, retirement_reversal_rate, etc. (measurement-and-scope.md §16).
- Risk-adjusted views derived from causal_lift + cost; not a new primary metric.
- Retirement remains T2 human-gated or offline proposal only. No auto-retirement.
- Portable Goal Certificate: additive extension of existing hashed certificate; separate checker preserved; type-matched asks; Ask-Early history. Cross-domain (including trading) without structural change.

**T3 surfaces:** Promotion thresholds, active_cap, retirement rules remain non-modifiable by the system.

### WS4 — Physics-Inspired Measurement Pipeline (Item 7)
**File:** `docs/specifications/physics-measurement-pipeline.md`

- Monte Carlo closure tests on synthetic trajectories with known injected lift.
- Systematic uncertainty budget template (judge bias, model version, temperature, prompt variation).
- Blinded analysis protocol for a1 (human unblinding decision).
- Power analysis calculator (assumptions stated; never used to claim support when intervals span zero).
- Offline-capable on packets or fixtures.

**T3 surfaces:** Measurement pipeline code and judge isolation remain non-modifiable.

### WS5 — Anti-Corruption Layers & Non-Modifiable Measurement Plane (Items 8, 10, 16)
**File:** `docs/architecture/anti-corruption-layers.md`

Bounded contexts (explicit labels):
- Recertia: Goal Certificates + measurement (SSOT).
- WL8 / Recernis: fill SSOT only.
- MomentumField: signals as versioned T2 facts (read-only).
- GROVE / Grok-Bot: off-graph clients of Tower + POST /v1/runs only.

Data-ownership table (mandatory):
| Artifact | Writer | Reader | Path into RunState? | Tier |
|----------|--------|--------|---------------------|------|
| Offline week packet | Human / offline job | Dashboard / reviewer | No | Operator tooling |
| Annotation store | Operator UI | Operator UI | No | Operator tooling |
| Tabletop log | Human | Reviewer | No | Operator tooling |
| MetricReport extensions | Existing measurement | Dashboard | No (additive) | T3 measurement |
| Goal Certificate | Existing checker | Consumers | No | Existing |

No dual-write of fills. Client utterances never enter RunState.

### WS6 — Remaining medium-priority items (Ask-Early telemetry, cross-domain notes, etc.)
Light specifications or deferred notes. All subject to the same invariants.

## 6. Sequencing (documentation only, within the large sprint)

- Days 1–3: Continuity Kit schema + characterization test plan + honest dashboard data contract.
- Days 4–6: Operator OS boundaries, annotation store contract, Ask-Early UX rules.
- Days 7–9: Library-as-Portfolio, Portable Goal Certificate, anti-corruption data ownership.
- Days 10–12: Physics pipeline, remaining items, full cross-review against invariants and existing ADRs.
- End: Consolidated review. Quality gate: every spec contains T3 subsection + characterization requirement; invariants checklist passes.

## 7. Acceptance Criteria (architectural quality gates)

- All new specs reference existing contracts (MetricReport, Goal Certificate, week_close, measurement-and-scope.md, ADR-0005).
- Characterization tests defined that freeze current soak-tree week-close behavior.
- Data ownership table present for every new artifact.
- Explicit statement that no UI, packet, or annotation has a write path into RunState or T3 surfaces.
- Offline packets produce valid “not established” reports.
- Control-arm identity invariant stated and testable.
- Model-id isolation (solver vs verifier) check included.
- Non-goals restated and enforced.

## 8. Risks and Mitigations

- Spec drift from existing contracts → cross-references + characterization tests.
- Accidental RunState write paths → data ownership table + explicit “no import” language.
- Over-scoping into implementation → strict “plan and specs only” rule.
- Counting empty weeks → explicit non-counting rule in packet schema.
- Tree divergence confusion → current-state assessment restated.

## 9. Next Actions (quality-gated)

1. Human reviews this document and confirms branch placement (done).
2. Later implementation sprint (after human decision): create the individual specification files under `docs/specifications/` and `docs/architecture/` as listed above, each carrying the required T3 subsection.
3. Characterization fixtures created from the current soak tree (when host is available or from recorded packets).
4. No merge to main until human [F] and existing CI gates pass.

## 10. Non-Goals (reaffirmed)

- No 16th node or graph growth.
- No self-modification of measurers or constraints.
- No unparking of Evidence-Bound Transfer or full D2 literature inbox in this sprint.
- No auto-promotion or auto-retirement.
- No dual-write of fills.
- No live host traffic required for these specifications.
- No claiming of RW-GA or `ga_claimed = true`.

---

**End of hardened workplan.**  
This document is the final deliverable for the request. Place additional detailed specification files in follow-on commits on this branch as needed. All content is consistent with standing Recertia constraints and the it-architect double-check performed 2026-10-26.
