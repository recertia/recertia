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


class _ScratchModelError(Exception):
    """The model call failed before a command was recorded."""


def scratch_max_steps() -> int:
    from recertia.settings import load_settings

    return max(1, min(20, load_settings().scratch_max_steps))


def solve_scratch_observe_act(
    state: RunState, ctx: NodeContext, attempt_no: int
) -> NodeOutcome:
    """Bounded observe–act loop: model proposes → shell runs → model sees output."""

    assert ctx.model is not None and ctx.tools is not None
    from recertia.solver.command_policy import CommandPolicyError, enforce_model_shell

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

        def _step(step_prompt: str = prompt, seq: int = step) -> dict:
            # Model and shell share one operation so resume replays the recorded command.
            window = RuntimeWindow(ctx)
            try:
                response = ctx.model.complete(  # type: ignore[union-attr]
                    step_prompt,
                    system="Return a single shell command only.",
                )
            except Exception as exc:  # noqa: BLE001 — solver boundary
                raise _ScratchModelError(str(exc)) from exc
            text = response.text or ""
            command = text.strip().splitlines()[0].strip().strip("`") if text.strip() else ""
            if not command:
                return {"command": "", "empty": True, "usage": window.delta().as_dict()}
            try:
                command = enforce_model_shell(command)
            except CommandPolicyError as exc:
                return {
                    "command": command,
                    "refused": str(exc),
                    "usage": window.delta().as_dict(),
                }
            if writer:
                writer.event("tool", tool="shell", command=command, step=seq)
            invoked = ctx.tools.invoke(  # type: ignore[union-attr]
                "shell",
                {"command": command},
                workdir=ctx.workdir,
                step_id=f"scratch-{seq}",
                origin="model",
            )
            record_new_affordances(ctx, window)
            return {
                "command": command,
                "returncode": invoked.exit_code,
                "stdout": invoked.stdout,
                "stderr": invoked.stderr,
                "usage": window.delta().as_dict(),
            }

        try:
            result = ctx.op_once(step, _step)
        except _ScratchModelError as exc:
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
        meter.charge_delta(UsageDelta.from_dict(result.get("usage") or {}))
        command = str(result.get("command") or "")
        if result.get("empty") or not command:
            last_error = "empty command from model"
            continue
        if result.get("refused"):
            last_error = str(result["refused"])
            history.append(f"$ {command}\n[refused] {last_error}")
            if writer:
                writer.event("tool", tool="shell", command=command, refused=last_error)
            continue
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
