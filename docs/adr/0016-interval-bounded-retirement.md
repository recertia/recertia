# ADR-0016: Interval-bounded retirement

- **Status:** accepted
- **Date:** 2026-08-17
- **Amends:** [ADR-0006](0006-bounded-library-and-retirement.md) decision 2

## Context

[ADR-0006](0006-bounded-library-and-retirement.md) benches a skill when
`applications >= evidence_floor` and the point estimate `ĉ(s) ≤ −τ`. The Newcombe–Wilson
interval is already stored on `Contribution` (`interval_low` / `interval_high`) and is
what `classify_lift` uses for class-level claims. Retirement ignored it.

A point estimate of `−τ` with a wide interval is not evidence of harm. The harsh-retirement
ablation that landed *below* the no-skill floor ([`references.md`](../references.md) §1.2)
is exactly this failure: acting on a noisy negative number. Using the optimistic bound is
the same honesty rule the eval report already applies — a claim is established only when
the interval excludes the threshold.

Track A/B unified the predicate (`retirement_decision`) but kept the point-estimate test
so the walk could land without changing measurement semantics. This ADR is that change.

## Decision

1. **Bench when, and only when,** `applications >= evidence_floor` **and**
   `interval_high` is present **and** `interval_high < −τ`.
2. **No interval is no decision.** A missing estimate *or* a missing `interval_high` is
   `no_estimate` / `no_interval`. The skill is kept. Judge-only samples already produce
   `estimate is None` (Track B); they still cannot retire.
3. **The bound is strict.** `interval_high == −τ` is not confident harm. `τ = 0`
   (`HARSH_AUTONOMY`) benches only when the interval is entirely negative — the same
   test as `classify_lift` → `established_negative`.
4. **One predicate.** `retirement_decision` is still the only bench function.
   `propose_retirements` and `maybe_bench_on_contribution` stay adapters. They pass
   `interval_high` through; they do not re-derive a second interval.
5. **Restore is unchanged.** `restore_benched` remains an operator / Curator action,
   not an automatic flip when the interval widens.

## Non-goals

- Deleting the dual active-set path. That was RW-PC, shipped after the
  [Phase-2 measurement report](../architecture/portfolio-measurement.md).
- Interval-based *promotion*. Shadow → candidate still uses the point lift bar.
- Changing `evidence_floor` or `retirement_threshold` defaults.

## Consequences

- Existing skills with a negative point estimate and a wide interval stay active.
  That is the intended correction, not a regression.
- Tests that retired on `estimate <= −τ` without an interval must supply
  `interval_high < −τ` or they now expect `keep`.
- The floor property in ADR-0006 still holds: cap and `τ` remain finite. The interval
  rule makes retirement *harder*, which is the direction the harsh-ablation evidence
  requires.
