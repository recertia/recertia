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
    from recertia.settings import load_settings

    return max(1, min(20, load_settings().scratch_max_steps))


def solve_scratch_observe_act(
    state: RunState, ctx: NodeContext, attempt_no: int
) -> NodeOutcome:
    """Bounded observe–act loop: model proposes → shell runs → model sees output."""

    assert ctx.model is not None and ctx.tools is not None
    from recertia.solver.command_policy import CommandPolicyError, enforce_model_shell

    model = ctx.model
    tools = ctx.tools
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
        window = RuntimeWindow(ctx)

        def _step(prompt_text: str = prompt, seq: int = step) -> dict:
            response = model.complete(
                prompt_text,
                system="Return a single shell command only.",
            )
            lines = response.text.strip().splitlines()
            command = lines[0].strip().strip("`") if lines else ""
            payload: dict = {
                "command": command,
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "cost_usd": response.cost_usd,
            }
            if not command:
                payload["empty"] = True
                payload["usage"] = UsageDelta().as_dict()
                return payload
            try:
                command = enforce_model_shell(command)
            except CommandPolicyError as exc:
                payload["refused"] = str(exc)
                payload["usage"] = UsageDelta().as_dict()
                if writer:
                    writer.event("tool", tool="shell", command=command, refused=str(exc))
                return payload
            payload["command"] = command
            invoke_window = RuntimeWindow(ctx)
            if writer:
                writer.event("tool", tool="shell", command=command, step=seq)
            result = tools.invoke(
                "shell",
                {"command": command},
                workdir=ctx.workdir,
                step_id=f"scratch-{seq}",
                origin="model",
            )
            payload.update(
                {
                    "returncode": result.exit_code,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "usage": invoke_window.delta().as_dict(),
                }
            )
            return payload

        try:
            result = ctx.op_once(step, _step)
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

        # Charge from the stored result so resume of this hop still meters spend
        # (AttemptMeter is per node-entry and is not idempotent).
        meter.charge(
            tokens=int(result.get("prompt_tokens") or 0)
            + int(result.get("completion_tokens") or 0),
            cost_usd=float(result.get("cost_usd") or 0.0),
        )
        command = str(result.get("command") or "")
        if result.get("empty"):
            last_error = "empty command from model"
            continue
        if result.get("refused"):
            last_error = str(result["refused"])
            history.append(f"$ {command}\n[refused] {result['refused']}")
            continue

        meter.charge_delta(UsageDelta.from_dict(result.get("usage") or {}))
        record_new_affordances(ctx, window)
        history.append(
            f"$ {command}\nexit={result['returncode']}\n"
            f"stdout:\n{result['stdout'][-1500:]}\nstderr:\n{result['stderr'][-800:]}"
        )
        if result["returncode"] != 0:
            last_error = f"step {step} exited {result['returncode']}: {command}"
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
