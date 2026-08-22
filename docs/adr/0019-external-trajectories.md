# ADR-0019: External trajectories and optional computer-use backends

- **Status:** proposed
- **Date:** 2026-08-22
- **Related:** [ADR-0002](0002-plural-memory.md), [ADR-0003](0003-criteria-preregistration.md), [ADR-0004](0004-offline-improvement-plane.md), [ADR-0005](0005-self-modification-boundary.md), [ADR-0011](0011-trajectory-and-counterfactual-replay.md), [ADR-0012](0012-product-console-surfaces.md), [ADR-0015](0015-improvement-plane-search.md)
- **Companion:** [plan](../plans/2026-08-grok-bot-external-trajectories.md)

## Context

Grok Bot (and similar computer-using agents) produce high-value **teach-once** recordings, bug-repro evidence packs, playtest traces, and docs-audit diffs. Recertia must not become those products. Recertia is the measuring orchestrator: trajectories enter as candidate episodic material, distill under existing gates, and promote only after control-arm lift.

Standing named teammates, persistent VMs as a security boundary, auto-write of approved state, consumer/GTM surfaces, and a sixteenth graph node are out of scope ([ADR-0005](0005-self-modification-boundary.md), [ADR-0015](0015-improvement-plane-search.md)).

## Decision

1. External material enters only via the `TrajectoryImport` contract (`contracts/trajectory_import.py`) with complete provenance and environment. Incomplete imports are rejected.
2. Import is append-only. It never mutates an existing Recertia run.
3. `reexecutable=False` trajectories may be written to the episodic store. They **cannot** be promoted until a Recertia-side re-validation path exists.
4. Promotion still requires automatic validation + human review + positive control-arm lift with a Wilson interval. The system may not write approved state from the import path.
5. Three golden task classes are first-class: `bug-reproduction`, `playtest-operator`, `docs-auditor`. They must support memory-off control runs.
6. A long-lived computer is an optional `ExternalComputerExecutor` affordance, policy-gated (`improvement.long_lived_computer_backend` default **false**). Default execution remains `--rm` containers. The shared computer is never a Recertia security boundary.
7. Operator surface: import action + stuck-jobs / lift-by-task-class / redundancy projections. No Bot persona.

## Consequences

- Recertia can absorb computer-use goldens without diluting isolation or lift honesty.
- Control arms for UI goldens cost more; they are budgeted under `job_quota.computer_use_practice_share`.
- Import validation is a new rejection surface; tests pin incomplete-provenance rejects.

## Rollback

Any promotion that cannot be reversed, any control-arm that cannot be executed, any relaxation of default isolation, or any surface that looks like a standing Bot.
