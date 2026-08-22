# ADR-0018: Idle-state offloading for working-set residency

- **Status:** proposed
- **Date:** 2026-08-22
- **Related:** [ADR-0002](0002-plural-memory.md) (plural memory), [ADR-0004](0004-offline-improvement-plane.md) (offline improvement plane), [ADR-0005](0005-self-modification-boundary.md) (tiers), [ADR-0011](0011-trajectory-and-counterfactual-replay.md) (trajectory already on disk), [ADR-0015](0015-improvement-plane-search.md) (fifteen-node topology is T3)
- **Evidence:** Chang et al., "From LLM Inference to Agentic Workloads," [arXiv:2608.15127](https://arxiv.org/abs/2608.15127) (AgentSysBench)

## Context

AgentSysBench reports production-shaped agentic sessions that (1) peak at ~28 GB of *sandbox working-set*, (2) sit idle for minutes to hours between active steps, and (3) drop resident memory ~4.6× when that working-set is offloaded rather than held live. Tool-result caching on the same traces cut redundant search calls by 35.2%.

Those numbers do not describe Recertia's current process:

- Durable planes already live on SQLite / JSONL / blobs under `.recertia/` ([ADR-0002](0002-plural-memory.md)). Serialising the episodic store "to the durable store" is a no-op.
- The container backend is one-shot: `--rm`, `--memory 512m`, `--cpus 1`, bind-mounted host workdir (`src/recertia/solver/container.py`). Recertia does **not** hold a live 28 GB sandbox between hops.
- Trajectories are already append-only JSONL on disk, read on demand (`src/recertia/trajectory/store.py`; [ADR-0011](0011-trajectory-and-counterfactual-replay.md)).
- Retention already GC's aged snapshots, transcripts, and workspaces (`src/recertia/retention.py`). That is deletion, not resumable offload.

What *does* accumulate, and what this ADR is about:

1. Host workdirs and `WorkspaceSnapshot` trees for paused / async / Practice runs that are still resumable.
2. In-process retrieval index pages and large `RunState` fields (`route_log`, snapshot lists) kept resident between hops in the API worker.
3. Concurrent Practice density: N workdirs × N checkpoint blobs, not N live containers.

Without an explicit idle lifecycle, operators cannot tell productive RSS from idle holding cost, and Practice cannot scale session count without copying the AgentSysBench failure mode as soon as we keep sandboxes longer (Phase 3 of the systems plan).

## Decision

Introduce **residency control** for already-persisted working-set, not a second copy of durable memory.

1. Extend run / workspace status with an explicit `idle` lifecycle distinct from `finalize`. A run is idle when there is no in-flight graph hop and no pending tool subprocess beyond a configurable quiet threshold. `finalize` remains terminal ([ADR-0015](0015-improvement-plane-search.md): this is not a sixteenth node).
2. `offload()` / `restore()` on the **working-set surfaces** only:
   - idle host workdirs and cold `WorkspaceSnapshot` trees,
   - large checkpoint blobs not required for the next hop,
   - cold retrieval postings / vector pages not in the active bundle.
3. Evict the in-memory (or unpacked-on-disk) representation; keep a handle: path, content hash, byte size, offloaded_at.
4. On next hop, `resume`, or retrieve, restore transparently **before** the hop proceeds. Restore is a named telemetry span (`idle.restore`); it MUST NOT be folded into task-class step latency or `cost_per_solved_task`.
5. Always-hot: active skill set, current identity / policy, in-flight attempt, frozen criteria, short-term working context, eval harness. These are never eligible.

v1 pack trigger is the orchestrator pause (`max_steps` slice in `GraphOrchestrator._execute`), not a between-hop timer. `quiet_threshold_s` is the telemetry floor for counting `idle_gap_ms`; it does not itself pack. Packing between hops would add restore latency to every hop.

Offloading is optional and policy-controlled (`state_management` in `policy/default.json`, T2). It never mutates approved skill content, policy, criteria, or the durable versioned record. It only changes residency of already-persisted bytes.

Eligible planes are T0 derived/rebuildable surfaces (`recertia.workspace`, `recertia.retrieval`, `recertia.graph` checkpoints). Sandbox *policy* remains T3. If a future long-lived container is introduced, its pause/checkpoint path is an implementation of this ADR, not a new tier.

## Rationale

- Matches the AgentSysBench finding that matters (idle-but-live working-set), translated onto Recertia's actual execution model instead of cargo-culting their 28 GB sandbox.
- Preserves exact resumability required by async API runs and Practice jobs.
- Fits [ADR-0002](0002-plural-memory.md) (planes already split) and [ADR-0004](0004-offline-improvement-plane.md) (Practice density without touching Execution writes).
- Does not grow the graph ([ADR-0015](0015-improvement-plane-search.md)) and does not write approved state ([ADR-0005](0005-self-modification-boundary.md)).

## Consequences

**Positive**

- Lower peak RSS and higher concurrent Practice / async-run density once idle holding is the dominant term.
- Cost attribution: productive compute vs idle holding (`idle_holding_bytes` vs `productive_bytes`).
- A defined hook for a future long-lived sandbox, so Phase 3 cannot quietly reintroduce 28 GB sessions.

**Trade-offs / costs**

- Restore latency on cold access. Budget: < 5 % of median hop time, measured as `idle.restore`, not as solve latency. Mitigate with a tunable quiet threshold; predictive warm-up is a stretch, not a Phase 1 requirement.
- New policy keys. Untuned defaults must be safe: offload **off** until Phase 0 has a baseline.
- Snapshot / workdir round-trips must be hash-checked. A restore that does not match the handle hash is a `RoutingError`, not a silent continue.

**Non-goals**

- Does not re-serialise SQLite / JSONL stores that are already durable.
- Does not alter promotion, retirement, or "only keep what still works."
- Does not move any write of approved state onto the Execution plane.
- Does not replace Curator, Practice, Recertifier, or retention GC. Retention deletes; offload parks.
- Does not add a graph node, a serving proxy, or weight updates.
- Does not claim the AgentSysBench 4.6× until Recertia's own idle-heavy sessions are measured.

## Implementation sketch

- Checkpoint / run metadata: `lifecycle ∈ {running, idle, finalized}` on the existing checkpoint store (`src/recertia/graph/store.py`, `src/recertia/graph/engine.py`). Watcher lives in the API run worker (`src/recertia/workers/run_worker.py`), not a new node.
- Working-set packer: content-addressed tarball or reuse `workspace/snapshot.py`; handle records hash + bytes. Restore is unpack + hash verify.
- Retrieval: page-level unload of postings not in the last bundle; `retriever.search` faults them back in. Invalidate on index rebuild / skill promotion (already T0).
- Policy keys under `state_management` (T2): `idle_offload_enabled` (default false), `quiet_threshold_s`, `eligible_surfaces`, `restore_latency_budget_frac` (default 0.05).
- Metrics (telemetry spans/events, not a new subsystem): `offload_count`, `bytes_offloaded`, `restore_latency_ms`, `peak_rss_before`, `peak_rss_after`, `idle_holding_bytes`.
- Tests: round-trip snapshot fidelity (hash); resume after forced idle; RSS delta on an idle-heavy fixture; `recertia lift` non-regression on a golden class (Practice runs excluded from user-facing lift per ADR-0004).

## References

- Chang et al., "From LLM Inference to Agentic Workloads: Characterization and Implications for Serving Systems," arXiv:2608.15127. Six properties and state-offloading exploration. Map their sandbox-hold result onto Recertia workdirs / snapshots, not onto a live container Recertia does not keep.
- Existing Recertia paths: `solver/container.py`, `workspace/snapshot.py`, `trajectory/store.py`, `retention.py`, `graph/engine.py`, `workers/run_worker.py`.
