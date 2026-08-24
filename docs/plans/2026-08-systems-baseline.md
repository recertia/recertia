# Phase 0 systems baseline (fixture, not golden-class)

- **Date:** 2026-08-24
- **Status:** instrumentation landed; golden-class numbers still due
- **Policy:** `state_management.idle_offload_enabled` = false
- **ADR:** [ADR-0018](../adr/0018-idle-state-offloading.md)

This is the Phase 0 table the plan required before enabling offload. It is **not**
AgentSysBench's 4.6× and it is **not** a golden-task measurement. It is what the
new telemetry emits on the in-process fixtures in
`tests/unit/test_systems_phase0.py` plus one M0 walking-skeleton hop.

| Property | Fixture observation | Golden-class (repo-chore / …) |
| --- | --- | --- |
| 1. Non-LLM vs LLM hop share | retrieve 40 ms / plan 10 ms → non-LLM share 0.80 on the synthetic pair | *unmeasured* |
| 2. Heterogeneous affinity | `component_class` now tags hops: retrieve / validate / container / llm / control_plane | *unmeasured* |
| 3. Shifting bottlenecks | `node.finished.latency_ms` histogram per node | *unmeasured* |
| 4. Idle-but-live gap | `idle_gap_ms` on each hop; synthetic median 7.5 ms | *unmeasured* |
| 5. Control-plane token tax | `model.completed` role + hop tokens | *unmeasured* |
| 6. Tool/retrieve redundancy | exact-match read of the same file → redundancy_rate 0.50; cache then hits | *unmeasured* |
| Peak RSS | `rss_bytes` emitted; process RSS > 0 on Linux | *unmeasured* |
| Workdir bytes | 2048-byte blob counted | *unmeasured* |

`recertia systems` folds telemetry JSONL into this snapshot. Offload stays off
until a golden-class row exists. Cache is on for read-only tools; writes never
cache.

Do not import 3–4× as a target from this table.
