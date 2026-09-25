# High-Confidence Items Implementation Plan

**Date:** 2026-08-22  
**Status:** Implemented; review follow-up in [`2026-08-high-confidence-review-fixes.md`](2026-08-high-confidence-review-fixes.md)  
**Sources:**  
- Ye, Q. et al. (2026). *On the Fragility of Self-Improving Agents: Variance, Task Order, and Underspecification*. arXiv:2608.18066.  
- Zhao, W. et al. (2026). *Large Language Model Agents Are Not Always Faithful Self-Evolvers*. arXiv:2601.22436.  

**Scope:** Four high-confidence items that strengthen measurement integrity and condensed-memory quality in Recertia. No changes to the T3 self-modification boundary, the control-arm requirement, or the single-agent default.

---

## Executive Summary

| # | Item | Primary source | Confidence |
|---|------|----------------|------------|
| 1 | Multi-run variance + best–worst gap in the lift harness | Ye et al. | High |
| 2 | Causal interventions on condensed memory (faithfulness) | Zhao et al. | High |
| 3 | Tighten distillation + pre-promotion applicability filter | Ye et al. | High |
| 4 | Prefer specific, contextualized, pitfall-oriented skill content | Zhao et al. | High |

These items map onto existing surfaces: `cli/lift.py`, `evals/statistics.py` + `EvalStore`, trajectory events (ADR-0011), the distiller (`nodes/distill.py` + `distill/`), promotion/hygiene (`memory/procedural/promote.py`, `lint.py`, `hygiene.py`), and the authoring prior.

---

## Goals

1. Make lift claims variance-aware and order-robust.
2. Make condensed-memory *use* falsifiable (faithfulness), not merely correlated with success.
3. Raise the quality bar on what enters the library so inapplicable or vague skills are rejected before promotion.
4. Prefer specific, pitfall-oriented skill content over generic heuristics.

All changes remain scaffolding-only. No weight updates. No self-edit of the referee.

---

## Item 1 — Multi-run variance + best–worst gap in the lift harness

### Spec

- Extend `CausalLiftResult` (and the lift CLI report) with:
  - Per-arm: `std_dev`, `best_rate`, `worst_rate`, `best_worst_gap` (absolute points).
  - Across the set of independent runs that constitute the current window: same statistics for the lift estimate itself.
- Require a configurable minimum number of independent runs (default ≥ 5) before a lift status of `established_positive` / `established_negative` is allowed. Below the floor the status remains `insufficient_data` or a new `low_run_count` variant.
- Persist the per-run success vectors in `EvalStore` so the numbers are reproducible from the ledger.
- Surface in `recertia lift` output and in the integrity ledger entry for the snapshot.

### Implementation plan

1. **Contracts** (`contracts/eval.py`): add fields to `CausalLiftResult` and a new `RunVariance` helper model.
2. **Statistics** (`evals/statistics.py`): implement `run_variance(rates: Sequence[float]) -> RunVariance` and fold it into `causal_lift`.
3. **Store** (`evals/store.py`): store the list of per-run Bernoulli outcomes (or success rates) per arm/snapshot; expose them to the lift command.
4. **CLI** (`cli/lift.py`): print the new fields; refuse to claim “established” when run count < floor.
5. **Tests**: synthetic multi-run fixtures that produce known variance and gap; assert status classification changes correctly.

### Acceptance criteria

- `recertia lift --task-class X` reports variance and gap when ≥ 2 runs exist.
- Status language never claims established lift with fewer than the configured floor of independent runs.
- Numbers match a pure-Python recomputation from the stored success vectors.

### Risk / dependency

Low. Pure measurement extension. Depends only on existing ablation-arm data collection.

---

## Item 2 — Causal interventions on condensed memory (faithfulness)

### Spec

Define four controlled interventions on the retrieved skill bundle before it reaches the solver:

| Intervention | Effect on condensed memory |
|--------------|----------------------------|
| `empty`      | Replace skill body with empty / placeholder text |
| `corrupt`    | Mutate key fields (steps, failure_modes, preconditions) while preserving surface structure |
| `irrelevant` | Swap in a skill from a distant task class |
| `filler`     | Replace body with non-semantic tokens of similar length |

For each intervention:

- Run the same task set under the intervention.
- Record (a) first-attempt success / causal_lift relative to the normal retrieval baseline, and (b) behavioral change via the trajectory event stream (decision-level divergence rate, skill-application events that disappeared or appeared).
- Report a **faithfulness score**: fraction of runs in which the intervention produced a statistically detectable change in either success or trajectory.

Primary surface: a new CLI `recertia faithfulness` (or an extension of the existing ablation / probe machinery) that accepts a skill snapshot, a task-class sample, and the intervention set. Results land in `EvalStore` and the integrity ledger.

### Implementation plan

1. **Contracts**: new `FaithfulnessIntervention` enum + result model that carries both performance delta and trajectory divergence metrics.
2. **Retrieval intercept**: small, explicit hook in `retrieval/pipeline.py` (or a test-only override) that can replace the retrieved bundle with an intervened version. Must be gated so it cannot be enabled in production runs.
3. **Trajectory analysis** (`trajectory/` + `replay/`): compute divergence (edit distance or event-type Jaccard) between the normal trajectory and the intervened trajectory for the same goal.
4. **Harness** (`evals/` or new `evals/faithfulness.py`): orchestration that materializes the four interventions, runs the sample, and writes results.
5. **CLI**: `recertia faithfulness --task-class … --interventions empty,corrupt,… --trials N`.
6. **Tests**: unit tests on the intervention transformers; end-to-end test on a golden task that is known to use a skill.

### Acceptance criteria

- Intervention of a skill that is actually used produces a measurable drop in success *or* a trajectory divergence above a configured threshold.
- Intervention of a skill that is never applied produces near-zero divergence (sanity check).
- All intervention runs are tagged in the ledger and cannot be mistaken for normal lift data.
- Production path remains completely free of intervention code (import boundary or feature flag that is off by default).

### Risk / dependency

Medium engineering cost. Relies on the trajectory emitter already being complete (ADR-0011). Keep the intercept behind a hard gate so it cannot leak into live solves.

---

## Item 3 — Tighten distillation + pre-promotion applicability filter

### Spec

At distillation time the authoring prior and the distiller prompt must receive:

- Explicit environment constraints (execution backend, available tools, sandbox limits).
- The locked `TaskCriterion[]` (or a compact rubric summary) for the originating task class.

Before a draft skill can enter the promotion pipeline it must pass an **applicability gate**:

- Environment check: does the skill reference tools/APIs/actions that the current environment does not expose?
- Criterion check: does the skill’s claimed success conditions match or refine the locked criteria of the task class it claims to serve?
- Contagion check: is the skill a near-duplicate of a previously rejected or low-contribution skill (embedding + structural hash)?

Failure of any check records a structured rejection reason and routes the draft to the dead-end / review queue rather than the active library.

### Implementation plan

1. **Authoring prior & distiller** (`distill/prior.py`, `nodes/distill.py`, `distill/success.py` / `failure_clusters.py`): inject environment model + criterion summary into the synthesis context.
2. **Lint / hygiene** (`memory/procedural/lint.py`, `hygiene.py`): add three deterministic (or lightly model-assisted) checks listed above.
3. **Promote path** (`memory/procedural/promote.py`): enforce the gate before any status transition to `candidate` / `approved`.
4. **Contracts**: extend skill schema with optional `applicability_report` and rejection reason codes.
5. **Tests**: synthetic skills that violate each rule must be rejected; valid skills must pass.

### Acceptance criteria

- A skill that recommends an unavailable tool is rejected with a clear reason.
- A skill whose success claim cannot be evaluated by the locked criteria is rejected.
- Rejection is recorded in the ledger and does not increase `library_yield` or active-set size.
- Existing golden skills continue to pass the new gate.

### Risk / dependency

Low–medium. Builds on already-present lint and promotion gates. Environment model must stay in sync with the execution backend configuration.

---

## Item 4 — Prefer specific, contextualized, pitfall-oriented skill content

### Spec

Update the authoring prior and the distiller prompts so that:

- Generic step lists are discouraged.
- `failure_modes` (or equivalent) is required and must be concrete (condition → observed failure → recovery).
- Preconditions and environment assumptions are first-class fields.
- Vague language (“be careful”, “handle edge cases”) is flagged by the lint pass.

This is a content-policy change, not a new runtime mechanism. It is enforced by the same lint/hygiene surface used in Item 3.

### Implementation plan

1. Revise the versioned authoring prior text (the meta-skill that guides distillation).
2. Add deterministic lint rules that score specificity (presence of concrete failure_modes, absence of banned vague phrases, presence of explicit preconditions).
3. Make the score part of the promotion quality gate (existing contribution / evidence floor remains primary).
4. Back-fill: optional one-time curator job that re-lints the current active set and demotes or flags low-specificity skills for human review.
5. Tests: prompt unit tests + lint unit tests on known good/bad skill examples.

### Acceptance criteria

- New skills distilled after the prior change contain concrete `failure_modes` in ≥ 90 % of successful distillations on the golden set.
- Lint rejects a hand-crafted vague skill.
- No regression in existing lift numbers on the control set of already-promoted skills.

### Risk / dependency

Low. Purely improves the content that Items 1–3 then measure and filter.

---

## Sequencing & Milestones

| Phase | Items | Deliverable | Est. effort |
|-------|-------|-------------|-------------|
| **P0** | 1 + 4 | Variance-aware lift + improved authoring prior + specificity lint | 3–5 days |
| **P1** | 3 | Environment + criterion injection into distiller + applicability gate | 4–6 days |
| **P2** | 2 | Faithfulness intervention harness + trajectory divergence metrics | 7–10 days |

P0 can ship independently and immediately improves honesty of every lift report.  
P1 hardens the library against the exact failure modes Ye et al. observed.  
P2 turns Zhao et al.’s critique into a continuous internal diagnostic.

---

## Shared Infrastructure Notes

- All new metrics and rejection reasons must be written to the integrity ledger.
- Feature flags / config for intervention mode and the run-count floor live under the existing policy surface.
- No changes to the T0–T3 boundary or to the “retrieve before invent” invariant.
- Golden evals and the control arm remain the final arbiters; these items only make the measurements more rigorous and the stored knowledge higher quality.

---

## References

- Ye, Qinyuan, Yu Li, Yada Pruksachatkun, Jiaxin Zhang, and Chien-Sheng Wu. 2026. “On the Fragility of Self-Improving Agents: Variance, Task Order, and Underspecification.” arXiv:2608.18066.
- Zhao, Weixiang, Yingshuo Wang, Yichen Zhang, Yang Deng, Yanyan Zhao, Wanxiang Che, Bing Qin, and Ting Liu. 2026. “Large Language Model Agents Are Not Always Faithful Self-Evolvers.” arXiv:2601.22436.
- Related internal: ADR-0011 (Trajectory events and counterfactual replay), `docs/architecture/measurement-and-scope.md`, `docs/references.md` §5.
