# ADR-0023: Trading skill changes stay proposals on the existing job plane

- **Status:** accepted
- **Date:** 2026-11-05
- **Accepted:** 2026-11-05. Number checked against `docs/adr/` on main `85994d369d82d3ee93c40e222aef14da9fd5d0aa`. The highest record was ADR-0022. 0023 was free.
- **Related:** [ADR-0003](0003-criteria-preregistration.md), [ADR-0004](0004-offline-improvement-plane.md), [ADR-0005](0005-self-modification-boundary.md), [ADR-0006](0006-bounded-library-and-retirement.md), [ADR-0010](0010-goal-as-primary-input.md)

## Context

A trading goal is still a Goal. Required outcomes stay `metric` criteria. The execution graph is not a place to add a trading node, a broker, or a second promotion path.

ATLAS (`chrisworsey55/atlas-gic`, MIT for the framework text, trained prompts excluded) suggested regime layers, performance-weighted influence, and gap-triggered specialists. Those ideas are useful only where Recertia already measures. They are not a license to copy prompts or to let a job write `approved`.

## Decision

1. The execution graph is unchanged. Fifteen nodes stay fifteen nodes.
2. Trading rewrites, gap spawns, cohorts, and retirements are proposals on the existing improvement plane. The new job name is `trading` (`trading_propose`). It is registered in `default_job_specs` and charged as `curator_retire`, the same budget class as `mine`. It does not call `write_candidate` or `submit_proposal`.
3. A retirement proposal is refused when contribution is null, evidence is missing, or contribution is not negative.
4. The paper tape is one official bar file. The default path is `data/official/aapl-plotly.csv`, overridden by `RECERTIA_OFFICIAL_BARS`. The file must start with `# official_bars v1` and name a `# source:`. Rows are not invented and a partial window is not dropped.
5. The locked paper gate stays 20 fills and 30 windows on each arm. The committed tape is shorter than that gate, so the job returns `horizon_remainder` and proposes nothing. That is a refusal, not a lift result.
6. The committed tape is the MIT-licensed Plotly AAPL chart (`finance-charts-apple.csv`, copyright 2019-2024 Plotly Technologies Inc.), retrieved 2026-11-05. It is not a brokerage feed. No Schwab, Stooq, or other vendor file is committed, because no redistribution license for one was available.
7. Influence weights and any regime blend stay derived stats. This ADR does not make them a T2 policy change.
8. No ATLAS prompt or ATLAS source file is copied into this repository.

## Consequences

- `recertia jobs run trading` can see the official tape and can build a `curate` proposal whose payload says `proposed`. It cannot promote.
- A longer official file, with two position streams, is still required before any lift claim. This ADR does not authorize orders.
