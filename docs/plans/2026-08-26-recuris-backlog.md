# Recuris extract backlog — 2026-08-26

**Status:** recorded, not scheduled. **Do not build.**
Source paper: Yu et al., arXiv:2608.24876 (Recuris). Record: [`2026-08-26-recuris.md`](2026-08-26-recuris.md).
IDs live in [`docs/architecture/remaining-work.md`](../architecture/remaining-work.md) §15.

## Inventory

| ID | Kind | Item | Build now |
| --- | --- | --- | --- |
| **RW-RC** | docs + research | Recuris as closest published sibling; no T3 change | this PR |
| **RW-RC1** | gated engineering | Checked working-memory commits on episodic + `validate` | no |
| **RW-RC2** | gated engineering | Four-way evolve blame taxonomy | no |
| **RW-RC3** | gated engineering | Lift / console three-arm: bare / $M_0$ / treatment | no |
| **RW-RC4** | gated engineering | Miner candidate `mined_from_paper:2608.24876` | no |
| **a10** | research | Checked WM + localized patches lift vs $M_0$ on `repo-chore` | untested |

## Hard constraints (copied onto every design PR)

1. Fifteen nodes stay T3. No 16th node.
2. No RSI language in product copy or ADRs.
3. No unbounded skill accretion. Recertifier + ADR-0006 cap stay load-bearing.
4. No SkillFlow-style in-family evolution as evidence of task transfer.
5. LLM-authored skills still face the SkillsBench bar (`curation` provenance, golden gate, lift).
6. Promotion of any mined Recuris skill requires Recertia `causal_lift`, not a paper table.
7. Enablement waits on `a1` interval **or** an explicit design review that re-opens RC* as a measurement experiment.

## Sequencing (when, not now)

```text
RW-RC   literature + IDs                         this change
RW-RC3  three-arm reporting on existing lift     after RW-M2 live eval rows exist
RW-RC1  checked WM on existing validate          after RC3 can show M0 vs treatment
RW-RC2  blame taxonomy on evolve / Curator       with RC1, not before
RW-RC4  miner candidate                          last; golden-gated; no auto-promote
a10     research outcome                         never a merge gate
```
