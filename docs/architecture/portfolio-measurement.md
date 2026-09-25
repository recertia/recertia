# Phase-2 portfolio measurement report

- **Status:** accepted
- **Date:** 2026-08-17
- **Closes:** RW-PC
- **Evidence:** `tests/unit/memory/test_portfolio_equivalence.py` (pre-expiry
  differential suite) and `tests/unit/memory/test_portfolio.py`

## Question

May the pure controller in `recertia.memory.procedural.portfolio` become the only
path through `recompute_active_set`?

ADR-0005 puts "which skills are retrievable" at T3. A dual implementation behind
`RECERTIA_PORTFOLIO_CONTROLLER` is therefore scaffolding, not operator config. It
survives only as long as it is proving something. This report is that proof.

## What was compared

Two implementations of the same contract, on the same fixture library, with and
without an eval store:

| Path | Ranking | Cap cut |
| --- | --- | --- |
| Legacy (`_recompute_active_set_legacy`) | `(estimate, trust)` then directory walk | slice |
| Controller (`rank_skills` + `select_active`) | estimate, trust, recency, applications, `(skill_id, version)` | first `cap` |

Shared orchestrator (`_pool_for_class`, `_apply_active_bits`):

- Refresh contribution from non-judge shadow/suppression when an eval store is
  present.
- Narrow the ranking pool to live-mix-eligible rows with a non-`None` estimate
  when that evidence exists; otherwise fall back to approved live-mix-eligible.
- Write active bits. Never bench. Cap pressure is
  `max(0, approved − cap) / cap`.

The fixture is not vacuous: the cap binds in `repo-chore`, bits flip, stats and
class-level ablation writes fire on the eval-store branch, and a second task
class with no randomized evidence exercises the fallback.

## Result

On every cap in `{0, 1, 2, 3, 4, 5, 50}` without an eval store, and on
`{0, 1, 3, 4, 50}` with one:

- returned statuses match
- pressure dicts match
- on-disk active bits match
- `write_status` / `write_stats` / `write_retrieval_ablation` sequences match

except the two ranking differences named below. Those are not regressions.
They are the spec.

## Intended differences (spec §24.1)

1. **Recency breaks a full estimate+trust tie.** Legacy inherited
   `list_versions` order (ascending `skill_id`). The controller consults
   `predictive_trust.last_used_at` next. A stale `aa-*` no longer beats a
   fresh `zz-*` for the last slot.
2. **Version is numeric.** Legacy inherited the directory walk's string order
   (`v10` before `v2`). The controller compares `version` as an integer, so
   `v2` precedes `v10`.

Neither difference changes who is *eligible*, who is *benched*, or how
pressure is computed. `recompute_active_set` still does not call
`propose_retirements`. Retirement stays on the Curator (Track B).

## Production default

The flag defaulted off, so production ran the legacy ranker. Folding the
controller in changes only those two tiebreaks. Both match
[library-authoring-and-concurrency.md](../specifications/library-authoring-and-concurrency.md)
§24.1. No other observable on the fixture moves.

## Decision

Delete `_recompute_active_set_legacy` and `RECERTIA_PORTFOLIO_CONTROLLER`.
`recompute_active_set` is the controller path. The dual implementation is no
longer evidence; it is a T3-adjacent knob.
