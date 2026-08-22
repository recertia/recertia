# ADR-0019: External trajectories and optional computer-use backends

- **Status:** proposed
- **Date:** 2026-08-22
- **Related:** [ADR-0002](0002-plural-memory.md), [ADR-0003](0003-criteria-preregistration.md), [ADR-0004](0004-offline-improvement-plane.md), [ADR-0005](0005-self-modification-boundary.md), [ADR-0011](0011-trajectory-and-counterfactual-replay.md), [ADR-0012](0012-product-console-surfaces.md), [ADR-0015](0015-improvement-plane-search.md)
- **Companion:** [plan](../plans/2026-08-grok-bot-external-trajectories.md)

## Context

Recertia writes durable memory only after automatic validation, human review, and measured lift against a memory-off control arm. It must degrade to a competent no-memory agent and must never treat an external agent's internal state as authoritative.

Teach-once recordings, bug-reproduction evidence packs, playtest UI sequences, and docs-auditor diffs are high-value trajectories currently outside Recertia's native runs. Some of those traces want a longer-lived computer than the default `--rm` container. Recertia absorbs the traces as candidate material; it does not become a Bot fleet.

## Decision

1. **TrajectoryImport** (`contracts/trajectory_import.py`) is the only ingest. Provenance (source, capture time, actor), environment, ordered steps, criteria snapshot, artifacts, and `reexecutable` are required as specified. Incomplete imports are rejected. Import is append-only and never mutates an existing Recertia run.
2. **Promotion path** is the existing Improvement-plane flow: episodic case → distill (candidate only) → review → control-arm measurement → possible `promote_to_approved`. No bypass of retrieve-before-invent, criteria lock, Wilson lift, or the performance floor. `reexecutable=false` stays episodic until a Recertia-side re-validation path exists.
3. **Optional `external_computer` tool** is registered in the default registry (`side_effect=external`). Default execution remains `--rm`, network-none, per-attempt workdir. Long-lived sessions are opt-in: `isolation.allow_external_computer`, `improvement.long_lived_computer_backend`, non-empty `isolation.external_computer_allowlist`, hard TTL. The shared computer is never a Recertia security boundary and never writes approved state. This build's handler is a **gate** — it refuses unless those flags are on, and even then does not open a standing VM (no live Grok Bot client).
4. **Golden task classes** (kebab-case, Recertia `task_class` pattern): `bug-reproduction`, `playtest-operator`, `docs-auditor`. Ordinary golden/eval material; same ablation and retirement rules; memory-off control runs required.
5. **No topology or product-surface expansion.** No new graph nodes, no multi-Bot fleet, no always-on named teammate. Practice density stays under `JobQuota.computer_use_practice_share`. Operator briefs (stuck jobs, lift, redundancy) are projections over Systems.

## Consequences

### Positive

- Computer-use trajectories enter episodic → procedural under Recertia's honesty layer.
- Isolation defaults and measurement integrity stay unchanged on the common path.

### Negative / trade-offs

- Control-arm trials on computer-use skills cost more; they start as shadow / JobQuota share.
- Import validation is a new rejection surface.
- Operators must treat any long-lived computer as an explicit exception.

### Neutral / unchanged

- Plane separation, review gate, versioned lineage, "only memory that still works gets kept."
- Degrades cleanly to a no-memory competent agent.

## Rollback

Any promotion that cannot be reversed, any control-arm that cannot be executed, any relaxation of default isolation, or any surface that looks like a standing Bot.
