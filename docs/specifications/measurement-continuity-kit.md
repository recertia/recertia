# Measurement Continuity Kit Specification

**Status:** Proposed (hardened, aligned to re-synthesis)  
**Date:** 2026-10-26  
**Branch:** docs/2030-sprint-workplan-hardened  
**Related:** Workplan section WS1; ADR-0005; measurement-integrity.md; week_close

## Purpose

Enable continued measurement, week-close validation, tabletop restore exercises, and honest interval reporting while the soak host is powered off. Characterization fixtures are the primary durable offline artifact that survives host power-off. All artifacts are offline-capable and do not count empty or fixture weeks toward RW-GA. Packets and reports default to “not established” when intervals span zero or data are sparse.

## Data Ownership

| Artifact | Writer | Reader | Path into RunState? | Tier |
|----------|--------|--------|---------------------|------|
| Offline week packet | Human or offline job | Dashboard, reviewer, characterization tests | No | Operator tooling |
| Characterization fixtures | Human | Test runner | No | Operator tooling |
| Tabletop log | Human | Reviewer | No | Operator tooling |

Packets are not consultable by retrieve. They are not part of the ablation arm. They never enter RunState or MemoryBundle. assumption_status fields are informational snapshots only; the human remains the sole writer of a1/a2/a4 status.

## Offline Week Packet Schema (additive)

```json
{
  "packet_schema_version": "1",
  "offline": true,
  "data_source": "fixture|recorded|synthetic",
  "eval_row_count": 0,
  "counts_toward_week": false,
  "metric_report": { /* existing MetricReport snapshot; field names must match current contract; includes floor-pressure metrics when present */ },
  "assumption_status": { "a1": "untested", "a2": "untested", "a4": "untested" },
  "tabletop_entries": [],
  "generated_at": "ISO-8601",
  "notes": "optional"
}
```

Empty or fixture packets set `counts_toward_week: false` and must not increment counted weeks. The schema is additive; existing week_packet fields remain valid. Floor-pressure metrics (library_yield, active_cap_pressure, retirement_reversal_rate) are carried when present in the source MetricReport.

## Characterization Tests (concrete outline)

Lock current soak-tree behavior for:

- `week_close` checker (including stop_plan conditions: two empty weeks, judge isolation violation, downward causal_lift, failed tabletop)
- `monday_close`
- `lift_label`

**Fixture structure (required):**
- `fixture_empty_week.json` — offline packet with `counts_toward_week: false`, zero eval rows. Test asserts the checker does not increment counted weeks and does not emit a silent zero for causal_lift.
- `fixture_recorded_packet.json` — snapshot of a real MetricReport. Test asserts Wilson interval and unavailable reasons are preserved exactly.
- `fixture_silent_zero_trap.json` — deliberately malformed packet missing interval fields. Test must fail (or label unavailable) rather than emit 0.0.

Fixtures live outside the live eval DB. Tests fail if an older packet is misinterpreted as having silent zeros or if counting rules change. Stop_plan conditions are asserted on synthetic packets that would have triggered them.

Additional required fixtures:
- Eval-firewall: offline packets must still satisfy existing rules (control arm present; no solver scoring its own artifact).
- Control-arm identity: fixture that fails if arms diverge on model version, temperature, or prompt beyond the deliberate ablation.
- Judge-isolation: offline check that fails if solver and verifier model ids match.

## Tabletop Restore Log Template

Required fields: date, run_id, restore_source, time_to_recover, follow_up. Satisfies existing completeness rules.

## Honest Interval Report

Consumes packet + MetricReport. Emits “not established” whenever a Wilson interval spans zero or data are sparse. Never silent zeros. Reuses existing unavailable reasons. “Not established” is the default design state while the host is off.

## T3 / Non-Modifiable Surfaces

Counting rules, stop_plan conditions, week-close checker logic, and measurement code remain non-modifiable by the system (ADR-0005). Characterization tests freeze them.

## Acceptance Criteria

- Existing week_close accepts offline packets and refuses to count empty/fixture weeks.
- Characterization tests pass against the frozen fixtures (empty, recorded, silent-zero trap).
- Silent-zero trap fixture fails rather than emitting 0.0.
- Tabletop template complete.
- Reports never emit silent zeros and default to “not established” when appropriate.
- No write path into RunState.
- Schema version present; older packets handled correctly.
- Floor-pressure metrics carried when present.
- Eval-firewall, control-arm identity, and judge-isolation fixtures pass.

## Non-Goals

No live traffic. No automatic counting of offline weeks. No graph changes. No unparking of EBT or D2.

## Cross-References

ADR-0005, measurement-and-scope.md, measurement-integrity.md, contracts for MetricReport and week packet.
