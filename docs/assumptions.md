# Assumptions register

This document tracks **empirical claims** the design depends on, separately from the
**engineering acceptance gates** in the shipped M0–M9 runtime. The
distinction is the fix for refactor-plan B7:

- An **engineering gate** asks "does the harness work?" — it can be satisfied by a system that
  correctly measures a null or negative result. It is a merge requirement.
- A **research outcome** asks "is the claim true, for us, at our traffic?" — it can only be
  answered by running the system and reading the number. It is never a merge requirement,
  because a correct implementation must be allowed to discover an inconvenient truth.

Every empirical claim below has a stable id (`a1`, `a2`, …) that milestones in
`archive/2026-Q3/implementation-plan.md` cite instead of silently assuming, plus a `status` field this
document keeps current as evidence accumulates. This register **migrates and supersedes**
[`references.md` §8](references.md#8-open-questions-the-literature-does-not-settle-for-us),
which remains in place only as a pointer here.

| Status | Meaning |
| --- | --- |
| `untested` | No run of ours has produced evidence either way |
| `under evaluation` | The relevant harness exists and is collecting data, interval not yet stable |
| `supported` | Our own measurement, not just the literature, confirms the claim within a stated interval |
| `refuted` | Our own measurement contradicts the claim; the dependent design surface needs revisiting |

---

## a1. Machine-checkable domains show a positive causal lift from skill retrieval

**Claim:** unlike SkillsBench's null result on general agent-skill tasks, repository-chore
tasks with tool-defined, machine-checkable success criteria will show positive `causal_lift`
from retrieval-augmented solving, measured against a sampled control arm with retrieval
suppressed (ADR-0003, [`references.md` §1.1](references.md#11-self-authored-skills-showed-no-benefit-curation-was-the-bottleneck)).

- **Depends on:** M4's ablation-arm harness (`archive/2026-Q3/implementation-plan.md` M4) computing
  `causal_lift` with a Wilson interval per task class.
- **Engineering gate (not this claim):** the harness correctly reports `causal_lift` and its
  interval, including reporting **"not established"** when the interval spans zero, verified
  against a synthetic scenario with a known, injected null effect (`archive/2026-Q3/implementation-plan.md` M4
  done-when).
- **Research outcome (this claim):** whether real `repo-chore` traffic actually shows a
  positive `causal_lift` with an interval excluding zero.
- **Status:** `under evaluation` — M4 harness exists and synthetic nulls are tested; real
  `repo-chore` traffic has not yet produced a stable interval.
- **Why it might be false anyway:** SkillsBench's null result may generalise if our curation
  bottleneck (§1.1) is not actually fixed by the review gate; machine-checkability narrows but
  does not eliminate that risk.

## a2. Ratchet's evidence floor is reachable at our traffic volume

**Claim:** an evidence floor of ~100 certification trials per skill before a skill is trusted
for unsupervised application (ADR-0006 default, drawn from Ratchet) is reachable within a
reasonable time window at our expected task volume.

- **Depends on:** M5's active-set and shadow-trial mechanisms (`archive/2026-Q3/implementation-plan.md` M5) and
  the Practice job feeding low-traffic skills synthetic curriculum tasks.
- **Engineering gate (not this claim):** the system enforces the floor correctly — skills below
  it are score-demoted rather than dropped, are excluded from unsupervised application, and
  Practice's synthetic trials are logged and counted toward the floor the same way real trials
  are (`archive/2026-Q3/implementation-plan.md` M5 done-when).
- **Research outcome (this claim):** whether real + practised trial volume actually clears the
  floor for most active skills within a reasonable time window, or whether the majority of the
  library sits permanently below it.
- **Status:** `under evaluation` — M5 autonomy / active-set / Practice mechanisms exist; no
  production skill has yet accumulated certification trials against the floor.
- **Why it might be false anyway:** Ratchet's own cold-start regime is admitted to be
  under-evidenced for traffic at our scale (`references.md` §1.1, §8 original note); our
  lower-volume setting may mean most skills sit below the floor indefinitely, which is itself a
  useful negative result, not a bug, if Practice cannot close the gap.

## a4. Judge false-pass bias stays below the threshold that disables retirement

**Claim:** with judge isolation (§26.3) and the contribution-estimate restriction to
non-`judge` criteria, the verifier's false-pass rate at our judge configurations stays below
the threshold past which contribution-based retirement silently disables (Blind Curator,
[`references.md` §1.8](references.md#18-a-biased-judge-silently-disables-retirement) **[B]**).

- **Depends on:** the Phase-1 verifier configuration and the Phase-2 judge false-pass canary:
  planted-failure
  artifacts scored by the verifier on a schedule, false-pass rate reported per model version.
- **Engineering gate (not this claim):** the canary harness exists and correctly measures a
  known, injected false-pass rate on synthetic artifacts, and reports a number per
  provider × model version on the real schedule.
- **Research outcome (this claim):** whether the measured rate at our judge configurations
  actually stays below the disabling threshold over time, per model version.
- **Status:** `untested` — isolation is enforced (necessary), but no false-pass measurement
  has ever been taken on this system (not sufficient).
- **Why it might be false anyway:** an isolated judge can still be false-pass-biased, and the
  Blind Curator result is that more data cannot cross the threshold once past it; model
  upgrades can move the rate in either direction without any code change on our side.

## a3. A tiered self-modification boundary is sufficient without an externally reported precedent

**Claim:** the T0–T3 self-modification boundary in [ADR-0005](adr/0005-self-modification-boundary.md)
is a sufficient safety surface for the classes of autonomous change this system permits
(policy tuning, retrieval threshold adjustment, distiller prompt revision) without letting the
system weaken the controls that measure or constrain it.

- **Depends on:** no specific milestone mechanism — this is a design-time claim tested by
  adversarial review and, eventually, by the Correction Miner's own change history staying
  inside T0–T2.
- **Engineering gate (not this claim):** T3 surfaces are structurally unreachable from
  improvement-plane code (import-boundary / capability tests per refactor-plan S6); every
  Correction Miner or Curator write is classified into a tier and logged to the integrity
  ledger with that tier.
- **Research outcome (this claim):** whether, over time, the boundary actually prevents the
  system from degrading its own measurement integrity, or whether some tier-2 surface turns out
  to have tier-0-equivalent leverage once composed with other tier-2 changes.
- **Status:** `untested` — the survey found no comparable system reporting a self-modification
  boundary of this kind, so there is no external validation to lean on either way
  (`references.md` §8 original note); we are ahead of reported practice here, which cuts both
  ways.

## a9. Condensed-memory interventions change Recertia behaviour when the skill is used

**Claim:** for a skill the solver actually applies, one of the four condensed-memory
interventions (`empty`, `corrupt`, `irrelevant`, `filler`) produces a statistically
detectable drop in first-attempt success or a decision-level trajectory divergence;
the same intervention on a skill that is never applied produces near-zero divergence
(Zhao et al. 2026; [`docs/plans/2026-08-high-confidence-review-fixes.md`](plans/2026-08-high-confidence-review-fixes.md)).

- **Depends on:** the P1 faithfulness writer in that plan (`run_intervened_trials` plus
  `IntervenedSkillStore` / `Retriever.bundle_hook` on eval fixtures only).
- **Engineering gate (not this claim):** the scorer does not treat missing intervention
  trials as detectable change; tagged `faithfulness:*` rows cannot enter lift; production
  retrieve never receives the hook. Verified by unit tests, not by a live-model result.
- **Research outcome (this claim):** whether Recertia's solver actually uses condensed
  skill bodies on `repo-chore` (then `research-synthesis`) traffic, or whether it ignores
  them the way Zhao et al. observed.
- **Status:** `under evaluation` — the P1 writer tags eval fixtures under
  `IntervenedSkillStore` / `bundle_hook`; live-model movement on `repo-chore` traffic
  has not produced a stable interval.
- **Why it might be false anyway:** Zhao's finding may generalise: the solver may lean
  on the raw trajectory / request more than the retrieved skill text, in which case
  interventions of used skills will also show near-zero divergence. That is a useful
  negative result, not a harness bug.

---

## a10. Checked working memory plus component-localized patches lift versus $M_0$ on repo-chore

**Claim:** on Recertia `repo-chore` tasks with locked machine-checkable criteria, a library
that (i) commits working-memory / episodic state only when validators plus environment
agree and (ii) attributes evolve patches to one of {skill, WM spec, invocation, checkers}
will show a `causal_lift` interval excluding zero against a neutral starter memory $M_0$
(no learned skills), and $M_0$ will not beat the bare / retrieval-suppressed arm
(Yu et al. 2026 Recuris; [`docs/plans/2026-08-26-recuris.md`](plans/2026-08-26-recuris.md)).

- **Depends on:** existing lift harness (bare vs treatment) plus a defined $M_0$ library
  snapshot; RW-RC1–RC3 if those ever ship.
- **Engineering gate (not this claim):** lift reports three arms and refuses
  `"established"` when any interval spans zero; $M_0$ is a real empty-or-seed snapshot,
  not a renamed treatment.
- **Research outcome (this claim):** whether Recertia traffic reproduces Recuris's
  "$M_0$ does nothing, evolved memory does" split on `repo-chore`.
- **Status:** `untested` — no Recertia three-arm run exists.
- **Why it might be false anyway:** Recuris gains are on $\tau^2$ / SkillFlow / Terminal-Bench,
  not `repo-chore`. SkillsBench self-authored skills were +0.0pp. Recuris does not recertify
  or cap the library; Recertia might spend the gain on hygiene instead of first-attempt
  success.

## Adding a new assumption

1. Assign the next `aN` id; state the claim as a specific, falsifiable sentence.
2. Name the milestone mechanism it depends on, and split its **engineering gate** (harness
   correctness, always a merge requirement) from its **research outcome** (whether the world
   cooperates, never a merge requirement) explicitly.
3. Set `status: untested` until a harness produces a number; update the status field, do not
   delete history — a claim moving from `supported` back to `refuted` after a regression is
   itself signal.
4. If a milestone done-when in `archive/2026-Q3/implementation-plan.md` would require this claim to be true to
   pass, that done-when is a bug (refactor-plan B7) — fix the done-when, not this register.
