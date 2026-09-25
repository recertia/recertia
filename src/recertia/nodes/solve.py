"""``solve``: apply a skill (or scratch) via the tool runtime and write a transcript (M2).

Fallback order:
1. Explicit ``ctx.script`` (tests / golden override) — still recorded into a transcript.
2. ``apply``/``adapt`` with a chosen skill + ``SkillApplicator`` (wave-based tool execution).
3. ``scratch`` via the model client proposing a shell command, executed through the tool runtime.
4. Legacy ``["true"]`` no-op only when no M2 services are configured (M0/M1 tests).
   With tools/transcripts wired, scratch without a model fails loud.
"""

from __future__ import annotations

from contracts.failure import FailureSignal
from contracts.run import RunState
from recertia.nodes._util import now
from recertia.nodes.attempt import AttemptMeter, failed
from recertia.nodes.context import NodeContext, NodeOutcome
from recertia.nodes.solve_apply import solve_via_applicator
from recertia.nodes.solve_branches import solve_branches
from recertia.nodes.solve_scratch import solve_scratch_observe_act
from recertia.nodes.solve_script import (
    ModelRequiredError,
    resolve_script,
    solve_legacy_script,
    solve_script_via_tools,
)


def solve(state: RunState, ctx: NodeContext) -> NodeOutcome:
    attempt_no = state.attempt_no + 1

    if state.branches and any(b.status == "dispatched" for b in state.branches):
        return solve_branches(state, ctx, attempt_no)

    if ctx.applicator is not None and state.strategy in ("apply", "adapt") and state.chosen and ctx.store:
        return solve_via_applicator(state, ctx, attempt_no)

    if (
        ctx.script is None
        and state.strategy == "scratch"
        and ctx.model is not None
        and ctx.tools is not None
        and ctx.transcripts is not None
    ):
        return solve_scratch_observe_act(state, ctx, attempt_no)

    try:
        script = resolve_script(state, ctx)
    except ModelRequiredError as exc:
        return failed(
            state,
            AttemptMeter.open(state),
            signal=FailureSignal(
                source="solver",
                detail=f"environment: {exc}",
                at=now(),
                class_hint="environment",
            ),
            attempt_no=attempt_no,
            note="scratch requires a configured model",
        )

    if ctx.transcripts is not None and ctx.tools is not None:
        return solve_script_via_tools(state, ctx, attempt_no, script)

    return solve_legacy_script(state, ctx, attempt_no, script)
