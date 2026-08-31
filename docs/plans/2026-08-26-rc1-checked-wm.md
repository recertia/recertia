# RW-RC1 design — checked working-memory commits

**Do not build.** Design only. Parent: [`2026-08-26-recuris-backlog.md`](2026-08-26-recuris-backlog.md).
Paper: Yu et al. arXiv:2608.24876.

## Intent

Recuris working memory advances a goal only when checkers plus environment agree. Recertia already has `validate` and an episodic store. Map the rule onto those surfaces. Do not add a node.

## Target surfaces (existing)

- `validate` node and criterion objects (ADR-0003).
- Episodic cases / task-state if MEA audited task-state is on (default off).
- Ledger: who committed a status change and which criterion passed.

## Contract sketch (not implemented)

```text
EpisodicCommit =
  case_id, goal_id, from_status, to_status,
  evidence_refs[], criterion_ids[], env_observation_hash,
  committed_at, run_id
```

Rules:

1. `to_status in {done, blocked}` requires at least one non-`judge` criterion pass plus an environment observation hash.
2. Model text asserting "done" is not evidence.
3. Failed commit is a dead-end case, retrieved later (failures are knowledge).
4. No write to procedural skills from this path.

## Tests that would exist later

- Synthetic: solver says done, env observation missing → commit rejected.
- Synthetic: criterion pass + env hash → commit accepted, lineage on case version.
- Graph cardinality: `contracts.graph.NODES` length remains 15.

## Out of scope

Recuris WM spec language, a standing task-state daemon, any 16th node, enabling MEA by default.
