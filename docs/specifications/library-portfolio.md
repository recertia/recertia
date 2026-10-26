# Library-as-Portfolio Specification

**Status:** Proposed (completed after harder synthesis)  
**Date:** 2026-10-26  
**Related:** Workplan WS3 (Tier B); measurement-and-scope.md §16; harder re-synthesis

## Purpose

Treat skills (and later trading strategies) as a measurable portfolio whose compounding is tracked by risk-adjusted causal_lift without growing the graph.

## Metrics

Reuse existing only: library_yield, skill_contribution, active_cap_pressure, retirement_reversal_rate. Risk-adjusted lift is derived from existing causal_lift and cost; it is not a new primary metric. Honest unavailable paths required when data is sparse.

**Display-only rule:** Any risk-adjusted view is displayed only. It is never stored in MetricReport and never used as a promotion or retirement input. Floor-pressure metrics remain primary.

Budget allocation is offline proposal only; human [F]/[B] required. No auto-allocation or auto-retirement.

## Trading Extension

Trading strategies treated as a parallel portfolio with the same metrics. Parameter surface exploration results are offline proposals scored solely by Recertia causal_lift / implicit recovery. They do not create new task classes or graph edges. Cross-domain transfer uses existing Goal Transfer mechanisms; no new nodes. Strictly gated by zero structural change and evidence gates.

## T3 / Non-Modifiable Surfaces

Promotion thresholds, active_cap, and retirement rules remain non-modifiable by the system.

## Acceptance Criteria

- Specs reference measurement-and-scope.md metrics.
- Risk-adjusted lift is display-only and never a promotion input.
- No auto-retirement.
- Offline proposals only.
- Additive schema with characterization tests.

## Non-Goals

No auto-promotion. No new retirement rules that bypass existing contribution mechanics. No graph changes.

## Cross-References

measurement-and-scope.md, ADR-0005, Goal Certificate, harder re-synthesis.
