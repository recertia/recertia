"""Bounded observe–act scratch loop. Owns command_policy."""

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
from recertia.solver.transcript import TranscriptWriter


def scratch_max_steps() -> int:
    import os

    raw = os.environ.get("RECERTIA_SCRATCH_MAX_STEPS", "5")
    try:
        return max(1, min(20, int(raw)))
    except ValueError:
        return 5


def solve_scratch_observe_act(
    state: RunState, ctx: NodeContext, attempt_no: int
) -> NodeOutcome:
    """Bounded observe–act loop: model proposes → shell runs → model sees output."""

    assert ctx.model is not None and ctx.tools is not None
    from recertia.solver.command_policy import CommandPolicyError, assert_command_allowed

    writer = (
        TranscriptWriter(ctx.transcripts, ctx.run_id, attempt_no)
        if ctx.transcripts is not None
        else None
    )
    history: list[str] = []
    meter = AttemptMeter.open(state)
    last_error: str | None = None
    max_steps = scratch_max_steps()

    for step in range(max_steps):
        exhausted = meter.preflight(tool_calls=1)
        if exhausted is not None:
            return failed(
                state,
                meter,
                signal=FailureSignal(
                    source="solver",
                    detail=f"budget exhausted before tool dispatch: {exhausted}",
                    at=now(),
                    class_hint="budget",
                ),
                attempt_no=attempt_no,
                note=f"scratch budget exhausted: {exhausted}",
                updates={"transcript_ref": writer.finalize() if writer else None},
            )

        history_block = "\n".join(history[-6:]) if history else "(no prior steps)"
        prompt = (
            f"Task: {state.task.request}\n"
            f"Workspace: {ctx.workdir}\n"
            f"Prior steps:\n{history_block}\n"
            "Propose exactly one shell command that progresses the task. "
            "Reply with only the command, no markdown. "
            "If the task appears done, reply with: true"
        )
        try:
            response = ctx.model.complete(
                prompt,
                system="Return a single shell command only.",
            )
        except Exception as exc:  # noqa: BLE001 — solver boundary
            return failed(
                state,
                meter,
                signal=FailureSignal(
                    source="solver",
                    detail=f"environment: model error during scratch: {exc}",
                    at=now(),
                    class_hint="environment",
                ),
                attempt_no=attempt_no,
                note="scratch model error",
                updates={"transcript_ref": writer.finalize() if writer else None},
            )
        # The model call is outside op_once, so it is re-issued on resume and charged directly.
        meter.charge(
            tokens=response.prompt_tokens + response.completion_tokens,
            cost_usd=response.cost_usd,
        )
        command = response.text.strip().splitlines()[0].strip().strip("`")
        if not command:
            last_error = "empty command from model"
            continue
        try:
            command = assert_command_allowed(command)
        except CommandPolicyError as exc:
            last_error = str(exc)
            history.append(f"$ {command}\n[refused] {exc}")
            if writer:
                writer.event("tool", tool="shell", command=command, refused=str(exc))
            continue

        window = RuntimeWindow(ctx)

        def _run(cmd: str = command, seq: int = step, win: RuntimeWindow = window) -> dict:
            if writer:
                writer.event("tool", tool="shell", command=cmd, step=seq)
            result = ctx.tools.invoke(  # type: ignore[union-attr]
                "shell", {"command": cmd}, workdir=ctx.workdir, step_id=f"scratch-{seq}"
            )
            return {
                "returncode": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "usage": win.delta().as_dict(),
            }

        result = ctx.op_once(step, _run)
        meter.charge_delta(UsageDelta.from_dict(result.get("usage") or {}))
        record_new_affordances(ctx, window)
        history.append(
            f"$ {command}\nexit={result['returncode']}\n"
            f"stdout:\n{result['stdout'][-1500:]}\nstderr:\n{result['stderr'][-800:]}"
        )
        if result["returncode"] != 0:
            last_error = (
                f"step {step} exited {result['returncode']}: {command}"
            )
            continue
        # Successful command: leave the attempt for validate to judge.
        # (A final `true` also ends the loop cleanly.)
        return completed(
            state,
            meter,
            attempt_no=attempt_no,
            transcript_ref=writer.finalize() if writer else f"{ctx.run_id}/attempt-{attempt_no}",
            description="scratch observe-act transcript",
            note=f"scratch observe-act completed after {step + 1} step(s)",
        )

    return failed(
        state,
        meter,
        signal=FailureSignal(
            source="solver",
            detail=last_error or f"scratch observe-act exhausted {max_steps} steps",
            at=now(),
        ),
        attempt_no=attempt_no,
        note="scratch observe-act exhausted without success",
        updates={"transcript_ref": writer.finalize() if writer else None},
    )
