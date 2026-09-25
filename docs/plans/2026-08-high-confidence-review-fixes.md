# High-Confidence Review Fixes

**Date:** 2026-08-22
**Status:** Implemented
**Parent:** [`2026-08-high-confidence-items-implementation.md`](2026-08-high-confidence-items-implementation.md)
**Trigger:** code review of `feat/high-confidence-integrity` (commits `297dd8b`, `472397e`)

Follow-up to close honesty and completeness gaps in the Ye/Zhao high-confidence items.
Does not reopen item 1's trial-count definition of `independent_runs` (already documented
in architecture §11.5). Does not change the T0–T3 boundary or the production retrieve path.

---

## Executive Summary

The variance-aware lift path is mergeable. The faithfulness scorer can claim a perfect
score on missing data, the intervention runner is unplugged, and three gates (criterion,
contagion, curator) are looser or noisier than the parent plan specified.

| ID | Fix | Severity | Phase |
| --- | --- | --- | --- |
| F1 | Do not score empty intervention arms as detectable | High | P0 |
| F2 | Pair trajectories by fixture, not concatenated bags | High | P0 |
| F3 | Tighten `detectable_change` (normalized edit distance) | High | P0 |
| F4 | Drop dead `apply_intervention` inside the scorer | Medium | P0 |
| F5 | Pair `lift_variance` by `snapshot_id`; omit gap on Bernoulli fallback | Medium | P0 |
| F6 | Exclude `faithfulness:` rows from `contribution_samples` | Low | P0 |
| F7 | Exact criterion match; local evaluable-cert check at promote | Medium | P0 |
| F8 | Distill environment from `ctx.tools`, not always `default_registry()` | Medium | P0 |
| F9 | Contagion: structural hash is the hard fail; cosine is advisory | Medium | P0 |
| F10 | Curator: proposals only, no `lint_reject` on approved seeds | Medium | P0 |
| F11 | Faithfulness writer: actually run trials and tag observations | High | P1 |
| F12 | Wire `IntervenedSkillStore` + `bundle_hook` into that writer | High | P1 |

P0 is the merge bar for calling the measurement honest. P1 is the merge bar for calling
Zhao et al. shipped. Do not mark research assumption `a9` `under evaluation` until P1
lands.

---

## Goals

1. A faithfulness report with no intervened trials must not claim change.
2. Trajectory divergence is pairwise for the same fixture, not a bag of all runs.
3. Applicability and curator behaviour match the parent spec without false rejects or
   ledger spam.
4. `recertia faithfulness run --trials N` writes tagged eval observations through an
   overlay store / retrieve hook. Production bootstrap still never receives the hook.

---

## Assumptions

Recorded as engineering assumptions here. The one empirical claim is `a9` in
[`docs/assumptions.md`](../assumptions.md).

| ID | Claim | Kind | Notes |
| --- | --- | --- | --- |
| E1 | Scoring an arm with `trials == 0` as `detectable_change=True` is a harness defect, not a research result | engineering | F1 |
| E2 | Distill copies locked `TaskCriterion.run` / `.expr` / `.metric` onto skill certs, so exact match will not reject legitimate distillates | engineering | F7 |
| E3 | Promote/shadow cannot reconstruct the originating run's locked criteria; a local "at least one non-judge cert" check is the substitute | engineering | F7 |
| E4 | Structural-hash equality already catches the contagion the parent plan named; hashed 64-d cosine at 0.97 false-positives on `repo-chore` shell skills | engineering | F9 |
| E5 | Faithfulness execution uses eval fixtures only (`is_eval_fixture=True`); never production traffic | engineering | F11 |
| E6 | `independent_runs = min(treatment.trials, control.trials)` stays; this plan does not add a window-count floor | engineering | out of scope |
| **a9** | Intervening on a *used* condensed skill changes Recertia first-attempt success or trajectory; intervening on an unused skill does not | research | never a merge gate |

---

## Item F1–F4 — Faithfulness scorer honesty

### Spec

`evaluate_faithfulness` scores only arms that have at least one intervened trial.
An arm with `sample.trials == 0` is listed with `detectable_change=False` and a new
`scored: bool = False` (or omitted from the score denominator). Empty event lists are
not a trajectory change.

`FaithfulnessReport.score` is `None` when `scored_arms == 0`. Do not report `0.0` or
`1.0` as if the measurement ran. CLI prints `score=unavailable (no intervened trials)`
and still writes a `faithfulness_report` ledger entry with that evidence.

Trajectory comparison is **pairwise by `fixture_id`** (fallback: same `task_id` /
goal). For each pair, compute Jaccard and Levenshtein on that pair's event-kind
sequences. Aggregate with the median across pairs. If no pair can be formed, trajectory
evidence is unavailable; performance lift can still score the arm.

`detectable_change` is true iff any of:

- `lift.status` in `{established_positive, established_negative}`
- median pairwise Jaccard `<= 1.0 - jaccard_drop` (default `0.15`)
- median pairwise **normalized** edit distance `>= 0.15`, where
  `normalized = edit_distance / max(len(a), len(b), 1)`

Drop `min_edit_distance >= 1` as a raw count. A single extra event on a long trajectory
is not a decision-level change.

Remove the discarded `apply_intervention(...)` calls inside `evaluate_faithfulness`.
The scorer consumes already-collected vectors. Transformers stay in
`recertia.evals.interventions` and in the P1 writer.

`skill_used` remains a caller-supplied flag. CLI may set it from baseline rows that
reference the skill, but an unused-skill sanity case with identical events and no
performance delta must still score `0` on scored arms.

### Implementation

- [`src/recertia/evals/faithfulness.py`](../../src/recertia/evals/faithfulness.py):
  `evaluate_faithfulness`, `detectable_change`, new `pairwise_divergences`.
- [`contracts/faithfulness.py`](../../contracts/faithfulness.py): `scored: bool` on
  `FaithfulnessArmResult`; `score: float | None` on `FaithfulnessReport`; optional
  `scored_arms: int`.
- [`src/recertia/cli/faithfulness_cmd.py`](../../src/recertia/cli/faithfulness_cmd.py):
  stop concatenating all run kinds; pair by `fixture_id`; skip zero-trial arms in the
  score; do not require a donor unless an `irrelevant` arm will actually be scored or
  executed.
- Regenerate schemas (`python3 scripts/generate_schemas.py`).

### Tests

- Existing used-skill / unused-skill unit tests keep passing with non-zero trials.
- **New:** baseline rows present, intervention `trials=0` → `scored=False`,
  `detectable_change=False`, report `score is None`.
- **New:** concatenated-bag regression: 8 baseline terminals vs 0 intervention events
  must not yield score `1.0`.
- **New:** two fixtures, pairwise Jaccard 1.0 on each, different bag lengths → not
  detectable from trajectory.
- **New:** `evaluate_faithfulness(..., outcomes={"irrelevant": sample})` with `donor=None`
  does not raise (F4).
- CLI test: `recertia faithfulness run` with an empty eval DB prints unavailable, exit 0.

### Acceptance

- No path from "no intervention data" to a positive faithfulness score.
- Production retrieve still never imports this module.

---

## Item F5–F6 — Lift variance pairing and filter alignment

### Spec

`EvalStore.snapshot_rates` (or a sibling) returns rates **keyed by `snapshot_id`**.
`causal_lift` pairs treatment and control rates on the intersection of snapshot ids,
sorted. Unpaired snapshots contribute to per-arm variance but not to `lift_variance`.

`recertia lift` prints `best` / `worst` / `gap` only when `RunVariance.n_runs >= 2`
**and** the series came from snapshots (not the Bernoulli 0/1 fallback). Std-dev of the
Bernoulli vector may still print, labelled as observation-level, or be omitted. Gap of
`1.0` on mixed 0/1 outcomes must not appear as a multi-run gap.

`contribution_samples` / `contribution_samples_bulk` exclude
`strategy LIKE 'faithfulness:%'` the same way `arm_counts` and `MetricReport` already do.

Do not change `independent_runs` semantics.

### Implementation

- [`src/recertia/evals/store.py`](../../src/recertia/evals/store.py): keyed snapshot
  rates; SQL filter on contribution queries.
- [`src/recertia/evals/statistics.py`](../../src/recertia/evals/statistics.py): pair on
  ids when a mapping is provided; keep list-zip only as a deprecated fallback unused by
  the CLI.
- [`src/recertia/cli/lift.py`](../../src/recertia/cli/lift.py): `_echo_variance` takes a
  `kind` (`snapshot` vs `bernoulli`) and suppresses gap for Bernoulli.
- [`src/recertia/evals/metrics.py`](../../src/recertia/evals/metrics.py): same pairing
  if it currently zips `_arm_rate_series` lists.

### Tests

- Treatment snapshots `{s1,s2,s3}`, control `{s2,s3,s4}` → lift_variance uses `s2,s3`
  only.
- Single snapshot mixed 0/1 → CLI does not print `gap=1.0000`.
- Faithfulness-tagged shadow row does not enter `contribution_samples`.

### Acceptance

- Misaligned snapshot lists cannot invent a paired lift gap.
- All eval aggregations that feed retirement or lift ignore `faithfulness:` rows.

---

## Item F7–F8 — Criterion match and environment source

### Spec

`_criterion_ok`:

- If `locked` is empty or has no required non-judge criteria, return True (nothing to
  check).
- Otherwise the skill must have at least one non-judge `SkillCertificationCriterion`
  that **exactly** matches a required locked criterion on `kind` and the identifying
  field: `run`, `expr`, or `metric` (string equality, not substring).
- Judge-only certs never satisfy the gate.

Promote and shadow-advance cannot pass originating-run criteria. Substitute:

- Always run environment + contagion.
- If `locked_criteria` is omitted, require `len([c for c in version.certification_criteria if c.kind != "judge"]) >= 1`.
  A skill with only judge certs (or none) fails `criterion_ok`.

Distill already passes `locked_criteria=list(state.criteria)` — keep that, now with
exact match.

Environment model at distill: if `ctx.tools` is set, use `ctx.tools.names()` and the
current `RECERTIA_EXECUTION_BACKEND`. Otherwise fall back to `default_registry()`.
Add an optional `registry` / `tool_names` argument so tests do not have to patch
`default_registry`.

### Implementation

- [`src/recertia/memory/procedural/applicability.py`](../../src/recertia/memory/procedural/applicability.py)
- [`src/recertia/nodes/distill.py`](../../src/recertia/nodes/distill.py):
  `environment_model_from_registry(ctx.tools)` or a thin helper that reads `.names()`.
- [`src/recertia/memory/procedural/promote.py`](../../src/recertia/memory/procedural/promote.py)
  and [`src/recertia/review/lifecycle.py`](../../src/recertia/review/lifecycle.py): no
  new criteria store; local non-judge-cert rule only.

### Tests

- Locked `run="test -f pyproject.toml"` vs skill `run="true"` → `criterion_ok=False`
  (today this can pass via substring / kind match).
- Exact same `run` → True.
- Promote of a draft with empty `certification_criteria` → `PromotionError`.
- Distill with a `ToolRuntime` that omits `shell` rejects a `shell` step even if
  `default_registry()` would include it.

### Acceptance

- Kind-level or substring matches cannot pass the gate.
- Restricted runtimes are visible at distill.

---

## Item F9 — Contagion: hash hard, cosine advisory

### Spec

`_near_duplicate` returns True only on structural-hash equality.

Embedding cosine (`>= 0.97`, same task class, same tool list) becomes an **advisory**
reason: `ApplicabilityReason(check="contagion", message=...)` is recorded, but
`contagion_ok` stays True unless the hash matches. Do not block promotion on hashed
64-d BoW similarity.

If we later want a hard embedding gate, it needs a real embedding and a separate
decision, not this hashed index.

### Implementation

- [`src/recertia/memory/procedural/applicability.py`](../../src/recertia/memory/procedural/applicability.py):
  split `_contagion_ok` into hard hash vs advisory cosine. Report both in `reasons`
  when cosine fires, with a distinct message prefix `advisory:`.
- [`docs/architecture/measurement-integrity.md`](../architecture/measurement-integrity.md)
  §11.7: state the split.

### Tests

- Hash-identical benched sibling still fails the gate.
- Same task class + same tools + near-identical `skill_document` but different step
  intents → `ok=True`, reasons include an advisory contagion note.
- Update [`tests/unit/memory/test_applicability.py`](../../tests/unit/memory/test_applicability.py)
  `test_contagion_embedding_near_duplicate_rejected` to expect advisory, not reject.

### Acceptance

- New `repo-chore` shell skills are not blocked by a benched cousin with a similar title.

---

## Item F10 — Curator specificity backfill without ledger spam

### Spec

The curator pass remains a **review queue**, not a reject path:

- Keep emitting up to 8 `Proposal(kind="curate", payload.specificity=True)` for
  approved+active skills with `SPEC` / `VAGUE` findings.
- **Do not** append `lint_reject`. That action means the lint pipeline blocked a
  draft. Warning-level findings on approved seeds are not rejects.
- Dedup: skip a skill if an open proposal with `payload.specificity` already exists
  for that `skill_id@version`, or if the last curator pass already flagged the same
  finding-code set (store a hash on the proposal payload and skip matches).
- Optional ledger action, if we want a paper trail: add `specificity_review` to
  `LedgerAction` and the spec literal. Do not reuse `lint_reject`. If we add it,
  append at most once per skill+finding-hash.

Seeds stay warning-severity. No auto-demotion.

### Implementation

- [`src/recertia/jobs/workers.py`](../../src/recertia/jobs/workers.py)
  `curator_active_set_and_dedup`.
- If adding `specificity_review`: [`contracts/ledger.py`](../../contracts/ledger.py),
  spec §21, schema regen, `tests/unit/jobs/test_curator_retirement.py`.
- Prefer **no new ledger action** unless we need the paper trail; proposals-only is
  enough for P0.

### Tests

- Two consecutive curator runs against the seed library produce proposals on the first
  and **zero new ledger `lint_reject` entries** on either.
- Second run does not duplicate specificity proposals for the same skill+codes.
- Approved seed with empty `failure_modes` still lints as warning, not error.

### Acceptance

- Weekly curator cannot grow the hash chain with 8 `lint_reject` rows per cycle.

---

## Item F11–F12 — Faithfulness writer (P1)

### Spec

`recertia faithfulness run --trials N` (N > 0) executes, it does not only score:

1. Load the target skill and optional donor.
2. Select up to N eval fixtures for that `task_class` (golden dirs under
   `evals/golden/<task_class>/`, same source as `recertia eval run`).
3. For the unmodified baseline (if fewer than N stored non-faithfulness rows exist
   for this skill) and for each requested intervention:
   - Construct `GraphOrchestrator` with
     `store=IntervenedSkillStore(inner, ...)` and
     `retriever=Retriever(index, bundle_hook=bundle_hook_for(...))`.
   - Run each fixture as `is_eval_fixture=True`. Unique `run_id`, stable
     `fixture_id` shared across baseline and intervention arms of the same golden.
   - **Do not** pass `script=` from `script_from_skill`. That path bypasses retrieve
     and would measure "does the mutated script still exec", which is not Zhao.
   - On finalize, `EvalStore.append_run` with `strategy` forced to
     `faithfulness:<name>` (baseline rows keep the run's natural strategy and must
     **not** use the prefix). Always `is_eval_fixture=True`.
4. Then score as F1–F4.

`--trials 0` (default) remains the offline scorer of already-tagged rows, with F1
honesty. `--donor-skill-id` required only when `irrelevant` is in the set **and**
`trials > 0` or that arm has rows.

`Policy.faithfulness_interventions_enabled` stays `false` and is **not** read by
bootstrap or `Retriever`. Constructor injection remains the only gate. Add a unit
test that `bootstrap.py` source does not pass `bundle_hook`. Do not delete the flag
in this change (policy churn); document that it is a statement of production posture,
not a runtime switch.

Writer lives in `recertia.evals.faithfulness` (T3). It may import `graph`,
`retrieval`, and `SkillStore`. `nodes/` and `jobs/` still must not import it.
`tests/boundary/test_import_boundary.py` stays green.

### Implementation

- New helper `run_intervened_trials(...)` in
  [`src/recertia/evals/faithfulness.py`](../../src/recertia/evals/faithfulness.py)
  or a sibling `evals/faithfulness_run.py` (still T3-prefixed).
- `EvalStore.append_run(state, *, strategy_override=None, force_eval_fixture=False)`
  so the writer can tag without forging observations. Keep
  `record_observation` rejected.
- CLI: honour `--trials`; reuse golden discovery from
  [`src/recertia/evals/golden.py`](../../src/recertia/evals/golden.py) without using
  its `script_from_skill` path.
- [`src/recertia/governance/tiers.py`](../../src/recertia/governance/tiers.py): if a
  sibling module is added, prefix-tier it T3 and add to `T3_FORBIDDEN_FOR_RUNS_AND_JOBS`.

### Tests

- Writer unit test with a fake orchestrator/callback: N=1, one intervention → one
  tagged observation, `is_eval_fixture=True`, prefix `faithfulness:`, and that row is
  absent from `arm_counts`.
- Overlay test: `IntervenedSkillStore.get_version` after `apply_empty` is what the
  orchestrator would see; retrieve `bundle_hook_for(empty)` is a no-op (body change
  is the store's job); `bundle_hook_for(irrelevant)` swaps ids.
- Source assertion: `bootstrap.py` / retrieve node / `recertia skills search`
  construct `Retriever(` without `bundle_hook`.
- Do **not** require a live-model e2e for merge. A used-skill golden moving under
  intervention is `a9` (research), not this engineering gate.
- Boundary test still forbids `recertia.evals.faithfulness` from `nodes/` and `jobs/`.

### Acceptance

- `--trials N` with N>0 writes tagged rows that F1 can score.
- `--trials 0` never claims a score without those rows.
- Production retrieve path unchanged.

---

## Out of scope

- Redefining `independent_runs` as snapshot/window count (architecture §11.5).
- Rewriting contract files for whitespace.
- Freezing `Retriever.bundle_hook` as a private attribute (optional, not required).
- Auto-demoting seeds that fail specificity lint.
- A real (non-hashed) embedding model for contagion.

---

## Sequencing

| Phase | Items | Deliverable | Merge meaning |
| --- | --- | --- | --- |
| **P0** | F1–F10 | Honest scorer, paired lift gap, tighter gates, quiet curator | Measurement cannot lie; library gates match the parent spec |
| **P1** | F11–F12 | Writer + overlay on the eval path | Zhao harness actually runs; `a9` may move to `under evaluation` |

P0 can ship without P1. P1 without P0 must not ship (writer would feed a scorer that
treats missing pairs as change).

Suggested commit split: (1) F1–F4+F6 scorer/store filters, (2) F5 lift pairing,
(3) F7–F9 applicability, (4) F10 curator, (5) F11–F12 writer.

---

## Docs to update when implementing

- [`docs/architecture/measurement-integrity.md`](../architecture/measurement-integrity.md)
  §11.5 (gap only for ≥2 snapshots), §11.6 (empty arms, pairwise trajectories, writer),
  §11.7 (exact criteria, advisory cosine, curator proposals).
- [`docs/plans/2026-08-high-confidence-items-implementation.md`](2026-08-high-confidence-items-implementation.md)
  status line → implemented, review follow-up here.
- [`docs/architecture/remaining-work.md`](../architecture/remaining-work.md) inventory
  row `RW-HCI` (engineering; P0 then P1).
- [`CHANGELOG.md`](../../CHANGELOG.md) under Unreleased.
- Regenerate `docs/architecture2.md` and schemas.

---

## References

- Parent plan: [`2026-08-high-confidence-items-implementation.md`](2026-08-high-confidence-items-implementation.md)
- Ye et al. 2026, arXiv:2608.18066
- Zhao et al. 2026, arXiv:2601.22436
- ADR-0005, ADR-0011, `docs/architecture/measurement-integrity.md`
