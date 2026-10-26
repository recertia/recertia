# Operator Measurement Continuity and Honest Interval Dashboard Specification

**Status:** Proposed  
**Date:** 2026-10-26  
**Related:** Workplan WS2; product-console.md; MetricReport

## Purpose

Provide operator-facing surfaces for honest interval reporting, three-plane visibility, Ask-Early telemetry, and external annotation while the host is powered off. Pure reader of existing data.

## Data Ownership

Dashboard is a pure reader of MetricReport and week packets. Annotation store is separate operator tooling with zero write path into RunState, MemoryBundle, or CONSTRAINTS.md. No “apply” or “promote” actions.

## Surfaces

- Primary: implicit recovery and causal_lift with Wilson 95% intervals and explicit “established” / “not established” / “unavailable” badges.
- Control-arm comparison: delta with invariant that arms differ only by the deliberate ablation.
- Assumption panel: a1, a2, a4 status (read-only) with evidence links or “untested”.
- Three-plane timeline (execution / memory / improvement) with causal overlays only where MetricReport provides them.
- Ask-Early indicator: % of trajectory before first clarification (advisory only; does not alter locked Goal or criteria).
- Offline banner when data is from packets.
- Time-travel scrubber: read-only over existing RunState checkpoints.
- External annotation drawer: notes attached to run_id or checkpoint, stored separately.

Progressive disclosure: summary → intervals → raw MetricReport.

## T3 / Non-Modifiable Surfaces

No UI path can alter locked criteria, ablation rate, measurement code, or constraints. Operator OS is not a graph route.

## Acceptance Criteria

- All surfaces honor honest unavailable / not established.
- Control-arm identity is testable.
- Zero write path from UI into the graph.
- Offline packets produce a valid report.
- Characterization requirement: fixtures demonstrate honest labeling.

## Non-Goals

No new nodes. No writes to RunState or MemoryBundle. Annotations do not affect graph execution.

## Cross-References

ADR-0005, product-console.md, measurement-integrity.md, MetricReport schema.
