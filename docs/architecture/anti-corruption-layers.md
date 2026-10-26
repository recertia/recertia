# Anti-Corruption Layers and Non-Modifiable Measurement Plane

**Status:** Proposed (completed after harder synthesis)  
**Date:** 2026-10-26  
**Related:** Workplan WS5 (Tier A); ADR-0001, ADR-0002, ADR-0005; horizon note; harder re-synthesis

## Purpose

Define bounded contexts, data ownership, and fail-closed contracts that prevent plane collapse, dual-write, and self-modification of measurement while integrating related systems.

## Bounded Contexts

- **Recertia**: Goal Certificates + measurement SSOT. MetricReport and assumptions have human-only or offline-proposal write paths.
- **WL8 / Recernis**: fill SSOT only. No dual-write into RunState.
- **MomentumField**: signals as versioned T2 facts (read-only into memory).
- **GROVE / Grok-Bot**: off-graph clients of Tower + POST /v1/runs only. Client utterances never enter RunState.

## Data Ownership Table

| Artifact | Writer | Reader | Path into RunState? | Tier |
|----------|--------|--------|---------------------|------|
| Offline week packet | Human / offline job | Dashboard / reviewer | No | Operator tooling |
| Annotation store | Operator UI | Operator UI | No | Operator tooling |
| Tabletop log | Human | Reviewer | No | Operator tooling |
| MetricReport extensions | Existing measurement | Dashboard | No (additive) | T3 measurement |
| Goal Certificate | Existing checker | Consumers | No | Existing |

## Contracts

- Fail-closed gates on plane boundaries. POST /v1/runs rejects any payload containing fields outside the documented contract before RunState is constructed.
- Continuous auditor for judge isolation: solver and verifier model ids must differ. Offline fixture required that fails if model ids match.
- Living non-goals register with explicit stop conditions (negative or unestablished a1, failed characterization tests, control-arm divergence halt expansion).
- Phased exit criteria: four consecutive counted weeks + tabletop_ok as Phase 1 exit.
- Schema changes additive and versioned, accompanied by characterization tests.
- Control-arm identity invariant: arms identical except deliberate difference.

## T3 / Non-Modifiable Surfaces

Measurement, judge isolation, promotion thresholds, ablation rate, and graph topology remain T3 (human-authored only). System may not modify mechanisms that measure or constrain it.

## Acceptance Criteria

- Every new store or surface has explicit single-writer and no-RunState-write rule.
- Characterization tests lock existing week_close behavior.
- Model-id isolation fixture present and fails on matching model ids.
- POST /v1/runs fail-closed contract stated.
- Living non-goals register and stop conditions present.
- Non-goals restated.

## Non-Goals

No 16th node. No dual-write of fills. No client utterances in RunState. No unparking EBT or full D2. No auto-promotion. No claims from sparse data.

## Cross-References

ADR-0001 (graph with loops, no growth), ADR-0002 (plural memory), ADR-0005, measurement-and-scope.md, loops-and-graphs-horizon.md, harder re-synthesis.
