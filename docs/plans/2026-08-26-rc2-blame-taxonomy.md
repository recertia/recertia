# RW-RC2 design — four-way evolve blame

**Do not build.** Design only. Parent: [`2026-08-26-recuris-backlog.md`](2026-08-26-recuris-backlog.md).

## Intent

Recuris attributes a failed trace to one of {experiential skills, WM spec, invocation policy, checkers} before proposing a patch. Recertia `evolve` already has failure classes. Extend the taxonomy; do not add a Meta-Agent that can edit the gate.

## Mapping

| Recuris component | Recertia home |
| --- | --- |
| Experiential skills `E` | procedural `SkillVersion` |
| WM spec `W` | episodic / desired-state schema, not a new plane |
| Invocation policy `ρ` | `plan` strategy + retrieve score floor |
| Checkers `C` | criteria objects (T3: agent cannot rewrite required non-judge criteria) |

## Contract sketch

```text
Blame =
  run_id, failure_class,
  component: skill | wm_spec | invocation | checkers,
  evidence_from_trajectory[],
  proposed_patch_kind,
  gate_id
```

Rules:

1. A Curator/Practice proposal names exactly one `component`.
2. `component=checkers` proposals are T3-blocked if they weaken or delete a required non-`judge` criterion (ADR-0003, ADR-0005).
3. Patches still pass the golden gate. Blame is evidence, not admission.
4. The improver cannot author the gate that admits the patch.

## Out of scope

A standalone Meta-Agent process, editing `validate` internals from evolve, RSI copy.
