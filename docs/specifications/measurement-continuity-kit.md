# Measurement Continuity Kit Specification

**Status:** Proposed  
**Date:** 2026-10-26  
**Branch:** docs/2030-sprint-workplan-hardened  
**Related:** Workplan section WS1; ADR-0005; measurement-integrity.md; week_close

## Purpose

Enable continued measurement, week-close validation, tabletop restore exercises, and honest interval reporting while the soak host is powered off. All artifacts are offline-capable and do not count empty or fixture weeks toward RW-GA.

## Data Ownership

| Artifact | Writer | Reader | Path into RunState? | Tier |
|----------|--------|--------|---------------------|------|
| Offline week packet | Human or offline job | Dashboard, reviewer, characterization tests | No | Operator tooling |
| Characterization fixtures | Human | Test runner | No | Operator tooling |
| Tabletop log | Human | Reviewer | No | Operator tooling |

Packets are not consultable by retrieve. They are not part of the ablation arm. They never enter RunState or MemoryBundle.

## Offline Week Packet Schema (additive)

```json
{
  "offline": true,
  "data_source": "fixture|recorded|synthetic",
  "eval_row_count": 0,
  "counts_toward_week": false,
  "metric_report": { /* existing MetricReport snapshot */ },
  "assumption_status": { "a1": "untested", "a2": "untested", "a4": "untested" },
  "tabletop_entries": [],
  "generated_at": "ISO-8601",
  "notes": "optional"
}
```

Empty or fixture packets set `counts_toward_week: false` and must not increment counted weeks.

## Characterization Tests

Lock current soak-tree behavior for:

- `week_close` checker (including stop_plan conditions: two empty weeks, judge isolation violation, downward causal_lift, failed tabletop)
- `monday_close`
- `lift_label`

Fixtures live outside the live eval DB. Tests fail if an older packet is misinterpreted as having silent zeros or if counting rules change.

## Tabletop Restore Log Template

Required fields: date, run_id, restore_source, time_to_recover, follow_up. Satisfies existing completeness rules.

## Honest Interval Report

Consumes packet + MetricReport. Emits “not established” whenever a Wilson interval spans zero or data are sparse. Never silent zeros. Reuses existing unavailable reasons.

## T3 / Non-Modifiable Surfaces

Counting rules, stop_plan conditions, week-close checker logic, and measurement code remain non-modifiable by the system (ADR-0005). Characterization tests freeze them.

## Acceptance Criteria

- Existing week_close accepts offline packets and refuses to count empty/fixture weeks.
- Characterization tests pass against frozen fixtures.
- Tabletop template complete.
- Reports never emit silent zeros.
- No write path into RunState.

## Non-Goals

No live traffic. No automatic counting of offline weeks. No graph changes. No unparking of EBT or D2.

## Cross-References

ADR-0005, measurement-and-scope.md, measurement-integrity.md, contracts for MetricReport and week packet.
