# Contribution ledger (Proposed)

Status: Proposed. This note does not change `a1`, `a9`, or `a10`. It does not add a graph node, a job, or a weight update. Jobs may propose a retirement only from a cell this protocol marks eligible. They still do not write T3.

Base: `main` at `6cfacb2` (ADR-0020 Store Receipts, still Proposed).

## Why

`CausalLiftResult` is a difference of two independent proportions (`newcombe_wilson`). Specs already say a class control arm must not be reused as a per-skill baseline, and that a skill contrast is shadow versus that skill suppressed. `off_intent_activation` is already a field. Distill is already blocked on eval fixtures. `a4` (judge false-pass) is untested.

A library interval can exclude zero while one skill helps and another hurts. A skill that was retrieved is not a random skill. Leave-one-out after the router chose the bundle is not a skill effect: suppressing A lets the router substitute B. A pathway filter changes the estimand from intention-to-treat to per-protocol. One shuffle is still one order.

## Estimands

Primary (`a1`, unchanged in claim). On a pre-registered chore list, retrieval-on minus retrieval-suppressed, same criteria hash, same model pin, paired on `task_id`. Order is a stratum: the registered order, and the registered shuffles, reported as separate rows. The interval is the paired form (Newcombe for paired proportions, or a score interval on the discordant pairs). Pairs that agree carry no contribution. Status stays `not_established` if the interval includes zero, if independent runs are under five, or if the gain exists only in the fixed-order row. Success is a non-judge criterion only.

Secondary, opened only if the primary excludes zero. Before the run, register at most K skills. Freeze the bundle the router returned, then randomly suppress one member. For skill s in stratum g:

    tau(s, g) = P(success | s in frozen bundle, g) - P(success | s masked, g)

on the same task. Holm across the K pre-registered skills. A skill the solver never applied is `untested`, not zero. A success where the skill was retrieved and not applied is a per-protocol row, not a replacement of the primary. Cost and attempts are a column. A skill that helps inside noise and doubles attempts is not a keep.

Holdout chores are named before the run and never enter the candidate writer.

## Schema

Optional cells beside `CausalLiftResult`. Defaults empty. No new `LiftStatus` value.

- `SkillContrastCell`: `skill_id`, `stratum`, `order_arm` (`fixed` | `shuffled`), `n_paired`, `n_discordant_help`, `n_discordant_hurt`, `estimate`, `interval`, `status`, `pathway` (`applied` | `retrieved_unused` | `never_retrieved`), `estimand` (`itt` | `per_protocol`), `multiplicity` (`primary` | `secondary` | `exploratory`), `holdout`.
- `CausalLiftResult.skill_contrasts`: list of cells, default empty.
- `CausalLiftResult.order_policy`: `fixed` | `shuffled` | null.
- `CausalLiftResult.estimand`: `itt` | `per_protocol` | null.
- `CausalLiftResult.holdout_id`: string | null.

`RunVariance` stays where it is. Class baseline is not stored on the cell.

## Refusals

The report must not say:

- "The library helped, so this skill helped."
- "Skill s is established" from a class baseline, from a fixed-order-only gain, from a per-protocol row alone, from an exploratory cell, or from a retirement while `a4` is untested.

If every applied cell includes zero, `a1` stays not established.

A retirement proposal is eligible only when the secondary cell interval lies entirely below the registered threshold, the shuffle row agrees in sign, and `judge_false_pass_rate` is measured and under the disabling threshold. This PR does not turn that proposal into a write.

## Out of scope

Recuris working-memory editor, PrisMem, a sixteenth node, a model-pin splice, `backup --receipts`, and any status change for `a1`.
