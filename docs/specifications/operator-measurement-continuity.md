# Operator Measurement Continuity and Honest Interval Dashboard Specification

**Status:** Proposed (hardened)  
**Date:** 2026-10-26  
**Related:** Workplan WS2; product-console.md; MetricReport

## Purpose

Provide operator-facing surfaces for honest interval reporting, three-plane visibility, Ask-Early telemetry, and external annotation while the host is powered off. Pure reader of existing data.

## Data Ownership

Dashboard is a pure reader of MetricReport and week packets. Annotation store is separate operator tooling with zero write path into RunState, MemoryBundle, or CONSTRAINTS.md. No “apply” or “promote” actions.

## Surfaces

- Primary: implicit recovery and causal_lift with Wilson 95% intervals and explicit “established” / “not established” / “unavailable” badges. Badge is “not established” whenever the Wilson interval includes zero or data are sparse.
- Control-arm comparison: delta with invariant that arms differ only by the deliberate ablation. A characterization fixture must fail if arms diverge on model version, temperature, or prompt.
- Assumption panel: a1, a2, a4 status (read-only) with evidence links or “untested”.
- Three-plane timeline (execution / memory / improvement) with causal overlays only where MetricReport provides them.
- Ask-Early indicator: percentage of trajectory nodes visited before the first clarification event. Computed as (nodes before first clarification) / (total nodes in the walk). Unavailable if the trajectory does not contain a clarification event. Advisory only; does not alter locked Goal or criteria.
- Offline banner when data is from packets.
- Time-travel scrubber: read-only over existing RunState checkpoints. No replay that writes state.
- External annotation drawer: notes attached to run_id or checkpoint, stored separately.

Progressive disclosure: summary → intervals → raw MetricReport.

## Annotation Store Schema (minimal)

```json
{
  "annotation_id": "uuid",
  "run_id": "string",
  "checkpoint_hash": "optional string",
  "operator_id": "string",
  "timestamp": "ISO-8601",
  "body": "string",
  "hash": "sha256 of body"
}
```

No import path into RunState, MemoryBundle, or retrieve. Retention is operator-controlled. Store is not consultable by any graph node.

## T3 / Non-Modifiable Surfaces

No UI path can alter locked criteria, ablation rate, measurement code, or constraints. Operator OS is not a graph route.

## Acceptance Criteria

- All surfaces honor honest unavailable / not established.
- Control-arm identity is testable via a divergence fixture.
- Zero write path from UI into the graph.
- Offline packets produce a valid report.
- Annotation store has no import path into RunState.
- Characterization requirement: fixtures demonstrate honest labeling.

## Non-Goals

No new nodes. No writes to RunState or MemoryBundle. Annotations do not affect graph execution.

## Cross-References

ADR-0005, product-console.md, measurement-integrity.md, MetricReport schema.
