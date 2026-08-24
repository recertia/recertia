# ADR-0019: External trajectories and optional computer-use backends

- **Status:** accepted
- **Date:** 2026-08-22 (rewrite 2026-08-24 against shipped main)
- **Related:** [ADR-0002](0002-plural-memory.md), [ADR-0003](0003-criteria-preregistration.md), [ADR-0004](0004-offline-improvement-plane.md), [ADR-0005](0005-self-modification-boundary.md), [ADR-0011](0011-trajectory-and-counterfactual-replay.md), [ADR-0012](0012-product-console-surfaces.md), [ADR-0015](0015-improvement-plane-search.md)
- **Companion:** [plan](../plans/2026-08-22-external-trajectories-and-computer-use-goldens.md)

## Context

Recertia writes durable memory only after automatic validation, human review, and measured lift against a memory-off control arm. It must degrade to a competent no-memory agent and must never treat an external agent's internal state as authoritative.

Teach-once recordings, bug-reproduction evidence packs, playtest UI sequences, and docs-auditor diffs are high-value trajectories currently outside Recertia's native runs. Some of those traces want a longer-lived computer than the default `--rm` container. Recertia absorbs the traces as candidate material; it does not become a Bot fleet.

A draft on closed PR #33 duplicated `TrajectoryImport`, used kebab task-class names, and added `Policy.improvement.external_trajectory_import`. Main already shipped a different contract (#35) and a Phase 0 CLI (#39). This ADR binds to **that** contract.

## Decision

1. **TrajectoryImport** (`contracts/trajectory_import.py`, shipped) is the only ingest. `ProvenanceBundle` comes from `audited_task_state` (`source`, not `actor`). Incomplete provenance or an empty environment descriptor is rejected. Import is append-only and never mutates an existing Recertia run. There is **no** `Policy.external_trajectory_import` flag; the CLI/HTTP surface is the gate.
2. **Golden task classes** are snake_case, matching `ComputerUseTaskClass` and `evals/golden/{bug_reproduction,playtest_operator,docs_auditor}/`. Skill documents still use kebab `SkillVersion.task_class` (`bug-reproduction`); distill maps snake → kebab. Goldens stay eval-firewalled (`is_eval_fixture=True`) and are **not** on the repo-chore promotion gate.
3. **Promotion path** is the existing Improvement-plane flow: episodic case → distill (candidate only) → review → control-arm measurement → possible `promote_to_approved`. No bypass of retrieve-before-invent, criteria lock, Wilson lift, or the performance floor. `reexecutable=false` stays episodic. `import_may_promote` is informational; this path never writes `approved`.
4. **Distill** (`recertia trajectory distill`) authors a **candidate** from a reexecutable import that has replayable shell steps and a non-`true` command criterion. It refuses no-op skills. Distill does not promote.
5. **Pending proposal** is queued on ingest only when `import_may_promote` is true (reexecutable + auditor re-verify + criteria snapshot). Status is `pending`. Not approved.
6. **Optional `external_computer` tool** is registered (`side_effect=external`). Default execution remains `--rm`, network-none, per-attempt workdir. Long-lived sessions are opt-in via `isolation.allow_external_computer`, `isolation.long_lived_computer_backend`, non-empty `isolation.external_computer_allowlist`, hard TTL. The handler is a **gate**: it refuses unless those flags are on, and even then does not open a standing VM. Approved skill state is never written from this path.
7. **Practice density** stays under `JobQuota.computer_use_practice_share` (snake task classes). Operator briefs (stuck jobs, lift-by-class, redundancy) are projections over Systems. Computer-use lift language is "not established" until `min_independent_runs`.
8. **No topology or product-surface expansion.** No new graph nodes, no multi-Bot fleet, no always-on named teammate.

## Consequences

### Positive

- Computer-use trajectories enter episodic → procedural under Recertia's honesty layer, on the contract that already shipped.
- Isolation defaults and measurement integrity stay unchanged on the common path.

### Negative / trade-offs

- Control-arm trials on computer-use skills cost more; they start as shadow / JobQuota share.
- Import validation is a new rejection surface.
- Operators must treat any long-lived computer as an explicit exception.
- Snake (goldens / Goal) vs kebab (SkillVersion) requires an explicit map at distill time.

### Neutral / unchanged

- Plane separation, review gate, versioned lineage, "only memory that still works gets kept."
- Degrades cleanly to a no-memory competent agent.
- MEA three-layer activation remains default-off.

## Rollback

Any promotion that cannot be reversed, any control-arm that cannot be executed, any relaxation of default isolation, or any surface that looks like a standing Bot.
