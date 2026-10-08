# ADR-0021: Paired contribution ledger

- Status: Proposed
- Date: 2026-10-07
- Deciders: operator
- Related: ADR-0003, ADR-0006, ADR-0016, specs §19, library-lifecycle §7.2

## Context

`CausalLiftResult` is a difference of two independent proportions, method `newcombe_wilson`. Specs already forbid reusing the class control arm as a per-skill baseline. Contribution is shadow versus that skill suppressed. Retirement reads `interval_high < −τ`. `off_intent_activation` is already a field. Distill is already blocked on eval fixtures. `a1` is under evaluation. `a4` is untested. `a10` has no three-arm run.

A library interval can include zero while one skill helps and another hurts, or exclude zero because the tasks arrived in a friendly order. A skill retrieved by the router is not a random skill. Leave-one-out after the router chose the bundle is not a skill effect: suppressing one member lets the router substitute another. A pathway filter changes the estimand from intention-to-treat to per-protocol.

## Decision

Lock two estimands. Both are paired on `task_id`, same criteria hash, same model pin. Success is a required non-judge criterion only.

Primary, `a1`, unchanged in claim. Retrieval on minus retrieval suppressed, on a pre-registered chore list. Order is a stratum: the registered order, and K registered shuffles, reported as separate rows. The interval is the paired form. Pairs that agree carry no contribution. Status stays `not_established` if the interval includes zero, if independent runs are under five, or if the gain exists only in the fixed-order row.

Secondary opens only if the primary excludes zero. Before the run, register at most K skills. Freeze the bundle the router returned, then randomly suppress one member. For skill s in stratum g:

τ(s, g) = P(success | s in frozen bundle, g) − P(success | s masked, g)

on the same task. Holm across the K pre-registered skills. A skill the solver never applied is `untested`, not zero. A success where the skill was retrieved and not applied is a per-protocol row, not a replacement of the primary. A cell with only judge criteria is null. Cost and attempts are a column.

The report shape is `ContributionCell` on `CausalLiftResult`. Jobs may propose a retirement only from a secondary cell whose interval lies entirely below the registered threshold, whose shuffle row agrees in sign, and only after `a4` has a measured false-pass under the disabling threshold. They still do not write T3.

## Refusals

The report refuses these sentences:

- "The library helped, so this skill helped."
- "Skill s is established" from a class baseline, from a fixed-order-only gain, from a per-protocol row alone, from an exploratory cell, or from a retirement while `a4` is untested.
- Any established wording if every applied cell includes zero. `a1` stays not established.

## Consequences

This ADR does not add a node, change `ablation_rate`, turn on `backup --receipts`, or mark `causal_lift` established. Schema export and the paired estimator are follow-ups. A cell with no discordant pairs is `insufficient_data`, not a zero effect.

## Alternatives rejected

- Importing a meta-agent that edits the harness. The graph stays frozen.
- Online per-task suppression as a policy change. The mask is a report until a human accepts a retirement proposal.
- One shuffle. K is registered before the run.
