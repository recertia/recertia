# RW-RC3 design — bare / M0 / treatment lift

**Do not build.** Design only. Parent: [`2026-08-26-recuris-backlog.md`](2026-08-26-recuris-backlog.md).

## Intent

Recuris's useful measurement is not "memory on." It is: bare agent vs neutral starter memory $M_0$ vs evolved package. Recertia lift today is treatment vs retrieval-suppressed. Add $M_0$ as a first-class arm when (not now) live eval rows exist (RW-M2 ops).

## Arms

| Arm | Meaning |
| --- | --- |
| `bare` | existing control: retrieve suppressed / empty bundle |
| `m0` | seed / human-authored starter library only; no Recertia-evolved versions |
| `treatment` | current active set |

## Reporting

- Three Wilson intervals. `established` only if treatment−bare excludes zero **and** the claim being made names the comparison.
- If $M_0$ beats `bare` with an interval excluding zero, the harness—not the learned library—is the product. Record that; do not call it Recertifier lift.
- Console: one task class, three columns. No sparkline theater.

## CLI sketch (not implemented)

```text
recertia lift --task-class repo-chore --trials 10 --arms bare,m0,treatment
```

`$M_0` is a library snapshot id, pinned on the ledger. It is not "whatever was in memory last Tuesday."

## Out of scope

Publishing Recuris $\tau^2$ numbers as Recertia lift. Changing default `recertia lift` before RW-M2 has live eval rows.
