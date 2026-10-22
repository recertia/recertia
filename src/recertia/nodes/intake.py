"""``intake``: normalise the request/goal, lock criteria (critic if needed), record the manifest.

Variant B: when ``task.goal`` is present it is compiled to ``TaskCriterion[]`` and locked.
Legacy pure-``request`` path still works via the critic.
"""

from __future__ import annotations

from contracts.criteria import clamp_criterion_timeouts
from contracts.goal import compile_goal
from contracts.run import RunState
from recertia.nodes._util import criteria_hash, now
from recertia.nodes.context import NodeContext, NodeOutcome
from recertia.validation.critic import propose_criteria, refine_goal_criteria
from recertia.validation.freeze import seal_must_not_modify_criteria


def intake(state: RunState, ctx: NodeContext) -> NodeOutcome:
    """Lock the ``TaskCriterion`` set and record its hash in the manifest.

    Precedence:
    1. Caller-supplied Goal → compile to criteria
    2. Caller-supplied criteria already on state
    3. Critic from request (legacy)
    """

    criteria = list(state.criteria)
    note = None

    if state.task.goal is not None:
        criteria = compile_goal(state.task.goal, source="caller")
        criteria = seal_must_not_modify_criteria(
            criteria, goal=state.task.goal, workdir=ctx.workdir
        )
        # Ensure sensitivity proofs exist for required criteria.
        criteria = refine_goal_criteria(criteria)
        note = f"compiled goal → {len(criteria)} criterion(ies)"
    elif not criteria:
        request_text = state.task.request or ""
        criteria = propose_criteria(request_text)
        note = f"critic proposed {len(criteria)} criterion(ies) from request"

    if ctx.max_criterion_timeout_s is not None:
        criteria = clamp_criterion_timeouts(
            criteria, ceiling_s=ctx.max_criterion_timeout_s
        )

    manifest = state.manifest.model_copy(update={"criteria_hash": criteria_hash(criteria)})
    new_state = state.model_copy(
        update={"criteria": criteria, "manifest": manifest, "criteria_locked_at": now()}
    )
    return NodeOutcome(state=new_state, route="always", note=note)
