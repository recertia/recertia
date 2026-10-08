from __future__ import annotations

from pathlib import Path

from contracts.run import ReusabilityVerdict, RunState
from recertia.distill.holdout import distill_holdout_block
from recertia.nodes.context import NodeOutcome

_HOLDOUT_PROTOCOL = Path("docs/protocols/repo-chore-contribution.json")


def holdout_outcome(state: RunState) -> NodeOutcome | None:
    """Return a one_off outcome when the fixture id is a registered holdout.

    Missing protocol file is not a block. This does not write T3.
    """

    fixture_id = getattr(state.task, "fixture_id", None)
    reason = distill_holdout_block(fixture_id, _HOLDOUT_PROTOCOL)
    if reason is None:
        return None
    verdict = ReusabilityVerdict(
        verdict="one_off",
        parameterisable=False,
        context_free=True,
        checkable=True,
        not_duplicate=True,
        bounded=True,
        reason=reason,
    )
    return NodeOutcome(
        state=state.model_copy(update={"reusability": verdict, "draft": None, "facts_extracted": []}),
        route="one_off",
        note=verdict.reason,
    )
