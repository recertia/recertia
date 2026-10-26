# Library-as-Portfolio Specification

**Status:** Proposed  
**Date:** 2026-10-26  
**Related:** Workplan WS3; measurement-and-scope.md §16

## Purpose

Treat skills (and later trading strategies) as a measurable portfolio whose compounding is tracked by risk-adjusted causal_lift without growing the graph.

## Metrics

Reuse existing only: library_yield, skill_contribution, active_cap_pressure, retirement_reversal_rate. Risk-adjusted lift is derived from existing causal_lift and cost; it is not a new primary metric. Honest unavailable paths required when data is sparse.

Budget allocation is offline proposal only; human [F]/[B] required. No auto-allocation or auto-retirement.

## Trading Extension

Trading strategies treated as a parallel portfolio with the same metrics. Parameter surface exploration scored solely by Recertia causal_lift / implicit recovery. Cross-domain transfer uses existing Goal Transfer mechanisms; no new nodes.

## T3 / Non-Modifiable Surfaces

Promotion thresholds, active_cap, and retirement rules remain non-modifiable by the system.

## Acceptance Criteria

- Specs reference measurement-and-scope.md metrics.
- No auto-retirement.
- Offline proposals only.
- Additive schema with characterization tests.

## Non-Goals

No auto-promotion. No new retirement rules that bypass existing contribution mechanics. No graph changes.

## Cross-References

measurement-and-scope.md, ADR-0005, Goal Certificate.
