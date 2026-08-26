# RW-RC — Recuris extracts (deferred, do not build)

Sibling to [`remaining-work.md`](remaining-work.md). Fold into that file §15 on merge.
Do not treat this file as an implementation plan that authorizes code.

Yu et al., *Recursive Experiential–Working Memory Evolution for Long-Horizon Agent Harnesses*,
arXiv:2608.24876, 25 August 2026 **[F]**. Closest published sibling: frozen model, fixed
Meta-Agent, gated memory patches, $M_0$ vs evolved vs bare.

**Do not implement in this milestone.** IDs exist so the extract cannot vanish into chat.
Index: [`docs/plans/2026-08-26-recuris-backlog.md`](../plans/2026-08-26-recuris-backlog.md).
Technical design PRs (docs only): branch `docs/rw-recuris-designs`.

## Inventory row (for remaining-work.md §2)

| ID | Kind | Item | Status |
| --- | --- | --- | --- |
| **RW-RC** | docs + gated engineering | Recuris extracts (checked WM, blame taxonomy, three-arm lift, miner candidate). **Do not build.** | open, deferred |

## Extracts

| ID | Extract | Home | Explicitly not |
| --- | --- | --- | --- |
| **RW-RC1** | Checked working-memory commits | episodic store + existing `validate` | 16th node, Recuris WM runtime |
| **RW-RC2** | Four-way blame: skills / WM spec / invocation / checkers | `evolve` failure classes + Curator proposal kind | Meta-Agent that edits the gate |
| **RW-RC3** | Lift arms: bare / $M_0$ / treatment | `recertia lift` + console metrics | Claiming paper benches as Recertia lift |
| **RW-RC4** | Miner candidate tagged `mined_from_paper:2608.24876` | existing Miner + golden gate | Auto-promote, SkillFlow-as-transfer |

## Enablement predicate (all required) before any RC* code PR

1. `a1` has a Wilson interval on `repo-chore`, or a written design review re-opens RC* as a measurement experiment.
2. No new graph node. Contracts stay at fifteen T3 nodes.
3. Recertifier and ADR-0006 cap remain on. Recuris accretion is a decline, not a template.
4. Any mined skill is `candidate` until Recertia lift beats control.

**Research:** [`assumptions.md#a10`](../assumptions.md) — `untested`. Not a merge gate.

**Done when (docs):** this section exists; paper bib records `yu2026recuris`; miner tag is specified.
**Done when (engineering):** not this year unless the predicate fires.
