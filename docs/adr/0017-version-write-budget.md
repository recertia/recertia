# ADR-0017: Version-write budget is a first-class dimension

- **Status:** accepted
- **Date:** 2026-08-17

## Context

`Budget.max_versions_written` (default 2) and `Spend.versions_written` have been on the
contract since the budget split. Architecture docs say the cap is enforced at `store`.
It was not. `budget_excess` never read the dimension. `store` never incremented spend.
A run could author without bound while the meter reported `versions_written = 0`.

That is the same class of lie Track A/B closed for retrieve (stale index rebuilt itself)
and for contribution (judge-only samples minted an estimate). A documented bound that
does not fire is worse than no bound: operators read the default and believe the walk
is finite.

`store` is a terminal node (`store → finalize` only). Enforcing the cap must not add a
route or a sixteenth node ([ADR-0005](0005-self-modification-boundary.md),
[ADR-0015](0015-improvement-plane-search.md)).

## Decision

1. **`budget_excess` includes `versions_written`.** Same machinery as attempts and tool
   calls. `BudgetReservation` carries the dimension so a requested write is counted
   before it happens. The test is `spent + reserved + requested > max` (inclusive cap).
2. **`distill` is the named gate.** `_version_write_budget` runs after the eval / arm /
   existing-skill gates and before `_author_or_reject`. An exhausted write budget is
   `one_off` — not a new draft. The route already exists.
3. **`review` refuses an approval that cannot be stored.** Same predicate, existing
   `rejected` route. A policy yes does not override a spent cap.
4. **`store` is the hard stop.** A hop that would exceed raises. This is a leaked gate,
   not a soft skip. `store` still always finalises *after* a successful write.
5. **`charge_version_write` in `recertia.nodes.attempt` is the sole writer of
   `spent.versions_written`.** Attempt-scoped dimensions stay on `AttemptMeter`. The
   version counter is not attempt-scoped (writes happen at `store`, not `solve`) but
   it still must not be hand-rolled at the node. The spend-accounting AST fence stays
   closed.
6. **The orchestrator asserts the invariant** after every hop:
   `spent.versions_written <= budget.max_versions_written`. A violation is a
   `RoutingError`, not a swallowed skip.
7. **No `ROUTES` edit.** Distill `one_off` and review `rejected` already leave the
   write path.

## Non-goals

- Charging fact writes or ledger appends against this cap. The dimension is skill
  versions, matching the field name.
- Per-branch version caps. Fan-out copies the parent `max_versions_written`; the run
  spend is shared.
- Changing the default of 2.

## Consequences

- A run that already stored `max_versions_written` versions will not author another
  candidate. The solved work still finalises; it is recorded as one-off evidence.
- `max_versions_written = 0` is a finite cap that admits no writes.
- Schema regeneration is required (`BudgetReservation.versions_written`).
