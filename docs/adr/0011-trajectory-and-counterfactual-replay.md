# ADR-0011: Trajectory events and counterfactual replay for measurement integrity

- **Status:** accepted
- **Evidence base:** plan derived from [Yan et al., arXiv:2607.01120](https://arxiv.org/abs/2607.01120) systems substrate (ATDP / replay); see [references §1.9](../references.md#19-trajectory-events-are-the-missing-measurement-substrate--without-weight-updates). Adapted to Recertia scaffolding-only non-negotiables

## Context

Causal lift and contribution retirement depend on run-start ablation and golden gates. Those cannot answer whether a *library change* would have altered outcomes on traffic already observed. `RunState` is a live state object, not a decision event stream. The memory ledger records only storage mutations.

## Decision

1. Emit a first-class, append-only **trajectory event stream** per run at decision boundaries (`contracts/trajectory.py`).
2. Support three **replay modes** against stored trajectories under a candidate `WorldState`: `retrieval_only`, `validate_only`, `full_execution`.
3. Require **retrieval-only counterfactual evidence** on material Curator proposals that change the active set (config-gated).
4. Trajectory emission is performed by the **graph engine**, not by nodes. Replay modules MUST NOT be importable from `recertia.nodes` (boundary test).

## Consequences

- New packages: `src/recertia/trajectory/`, `src/recertia/replay/`.
- Schemas generated from contracts (ADR-0009).
- Golden gates remain mandatory for promote-to-approved; replay packs are additive evidence.
- Weight updates, multi-tenant proxies, and multi-surface evolution control planes remain out of scope (ADR-0005).
- Phase 3: `validate_only` re-derives success from `criterion_scored` events (optional live rescorer); `full_execution` is opt-in via `RECERTIA_ALLOW_FULL_REPLAY=1` + budget + optional `orchestrator_factory`.
