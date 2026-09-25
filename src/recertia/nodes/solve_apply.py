"""Applicator path: wave-based tool execution of a chosen skill."""

from __future__ import annotations

from contracts.failure import FailureSignal
from contracts.run import RunState
from recertia.nodes._util import now
from recertia.nodes.attempt import (
    AttemptMeter,
    RuntimeWindow,
    UsageDelta,
    completed,
    failed,
    record_new_affordances,
)
from recertia.nodes.context import NodeContext, NodeOutcome
from recertia.nodes.solve_script import fill_skill_params
from recertia.solver.transcript import TranscriptWriter


def solve_via_applicator(state: RunState, ctx: NodeContext, attempt_no: int) -> NodeOutcome:
    assert ctx.applicator is not None and ctx.store is not None and state.chosen is not None
    version = ctx.store.get_version(state.chosen.skill_id, state.chosen.version)
    params = fill_skill_params(version, state.chosen.bound_parameters)

    writer = (
        TranscriptWriter(ctx.transcripts, ctx.run_id, attempt_no)
        if ctx.transcripts is not None
        else None
    )
    # Local writer stub when transcripts aren't configured.
    if writer is None:
        from recertia.solver.transcript import TranscriptStore

        writer = TranscriptWriter(TranscriptStore(ctx.workdir / ".transcripts"), ctx.run_id, attempt_no)

    meter = AttemptMeter.open(state)
    window = RuntimeWindow(ctx)

    def _run() -> dict:
        result = ctx.applicator.apply(  # type: ignore[union-attr]
            version,
            params=params,
            workdir=ctx.workdir,
            run_id=ctx.run_id,
            attempt_no=attempt_no,
            transcript=writer,
        )
        # Persist a JSON-safe summary so at-least-once resume stays valid. Usage is measured
        # inside the operation so a replayed result still charges what it originally spent.
        return {
            "ok": result.ok,
            "transcript_ref": result.transcript_ref,
            "error": result.error,
            "merge_timeout": result.merge_timeout,
            "waves": [wr.wave.model_dump(mode="json") for wr in result.waves],
            "conflicts": [
                c.model_dump(mode="json") for wr in result.waves for c in wr.conflicts
            ],
            "usage": window.delta().as_dict(),
        }

    summary = ctx.op_once(0, _run)
    record_new_affordances(ctx, window)
    meter.charge_delta(UsageDelta.from_dict(summary.get("usage") or {}))

    from contracts.resources import ResourceConflict
    from contracts.run import StepWave

    waves = [StepWave.model_validate(w) for w in summary["waves"]]
    conflicts = [ResourceConflict.model_validate(c) for c in summary["conflicts"]]

    if not summary["ok"]:
        detail = summary["error"] or "skill application failed"
        if summary["merge_timeout"]:
            detail = f"claim timeout: {detail}"
        return failed(
            state,
            meter,
            signal=FailureSignal(source="solver", detail=detail, at=now()),
            attempt_no=attempt_no,
            note=f"applicator failed: {detail}",
            updates={
                "transcript_ref": summary["transcript_ref"],
                "step_waves": [*state.step_waves, *waves],
                "resource_conflicts": [*state.resource_conflicts, *conflicts],
            },
        )

    return completed(
        state,
        meter,
        attempt_no=attempt_no,
        transcript_ref=summary["transcript_ref"] or f"{ctx.run_id}/attempt-{attempt_no}",
        description="structured attempt transcript",
        updates={"step_waves": [*state.step_waves, *waves]},
    )
