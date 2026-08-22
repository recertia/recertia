# Technical Plan: Grok Bot Patterns → Recertia

Status: implementing (ADR-0019 companion). Recertia remains the sole measuring
orchestrator. No new planes, no new graph nodes, no always-on fleet.

## Five patterns (only)

1. Teach-once recording / solved run → promotion-gated skill
2. Bug Reproduction, Playtest Operator, Docs Auditor as golden task classes
3. Writes/spend always behind approval (already present)
4. Many practice runs + summarize under JobQuota
5. Operator brief: stuck jobs, lift, redundancy (Systems projection only)

## Non-goals

Standing teammates, persistent-VM default, auto-write of approved state,
consumer/GTM surfaces, graph growth past 15 nodes, any relaxation of
retrieve-before-invent / criteria lock / control-arm lift / plane separation.

## Phases

- **0** contracts + goldens + `recertia trajectory import` — done
- **1** import → episodic → pending proposal; `recertia trajectory distill` writes *candidate* only — done
- **2** operator projections + JobRunner `--task-class` quota charge; executor contract remains flag-off
- **3** cost control / retirement — not started

## Remaining

- `ExternalComputerExecutor` is not registered in the affordance store (flag stays false).
- No established control-arm lift on a computer-use class (honest “not established”).
- Phase 3 soak / retirement tuning.

