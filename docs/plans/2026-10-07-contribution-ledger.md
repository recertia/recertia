# Contribution ledger protocol

Status: Proposed. Not a measurement. `a1` stays under evaluation.

## Register before the first trial

- Task class: `repo-chore`.
- Criteria hash, model pin, seed policy.
- Chore list and stratum labels. Strata are pre-registered, not clustered after the run.
- Holdout ids. Those chores never enter distill.
- K skills, named. Skills found after the run are exploratory.
- K shuffle seeds.
- Retirement threshold τ, already the ADR-0016 bound. This protocol does not change it.
- `a4` canary: planted failures scored by the verifier. If the false-pass rate is missing, retirement proposals stay blocked.

## Primary

Paired on `task_id`. Arm A retrieval on, registered order. Arm B retrieval on, shuffle k. Arm C retrieval suppressed. Report A−C and B−C as separate rows. Do not pool them.

Interval: paired Newcombe, or a Wilson score interval on the discordant pairs. Concordant pairs do not contribute. Method string `paired_newcombe` so it is not confused with the unpaired class interval.

Status `not_established` if the interval includes zero, if independent runs are under five, or if only the fixed-order row excludes zero.

## Secondary

Opens only if the primary excludes zero in both order arms. Freeze the router bundle, then mask one registered member. Do not let the router refill the slot.

τ(s, g) on the same task. Holm across the K named skills. Pathway `applied`, `retrieved_unused`, or `never_retrieved`. Estimand `itt` or `per_protocol`. A `retrieved_unused` success is not an `itt` success for that skill.

## What this protocol does not do

- It does not write T3.
- It does not shrink `ablation_rate`.
- It does not open a second task class.
- It does not start `a10`.
- It does not treat a desk test as a Recertia row.
