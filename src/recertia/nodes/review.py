"""``review``: hygiene + golden gate + policy/human decision (specs §4, §8)."""

from __future__ import annotations

from contracts.budget import BudgetReservation, budget_excess
from contracts.run import RunState
from contracts.skill import SkillVersion
from recertia.nodes.context import NodeContext, NodeOutcome


def review(state: RunState, ctx: NodeContext) -> NodeOutcome:
    if not state.draft:
        return NodeOutcome(state=state, route="rejected", note="no draft to review")

    version = SkillVersion.model_validate(state.draft)
    if ctx.reviewer is None:
        # Do not invent a reviewer that can write into a shared skill store by accident.
        return NodeOutcome(
            state=state,
            route="rejected",
            note="no ReviewService configured; refusing to auto-approve into the skill store",
        )
    service = ctx.reviewer
    service.enqueue(version, run_id=ctx.run_id)
    decision = service.decide(version, run_id=ctx.run_id, reviewer="m3-policy")

    note = f"{decision.outcome}: {decision.note or ''}".strip()
    if decision.outcome == "approved":
        if (
            budget_excess(
                state.budget,
                state.spent,
                state.reserved,
                BudgetReservation(versions_written=1),
            )
            == "versions_written"
        ):
            return NodeOutcome(
                state=state,
                route="rejected",
                note="version write budget exhausted",
            )
        return NodeOutcome(state=state, route="approved", note=note)
    return NodeOutcome(state=state, route="rejected", note=note)
