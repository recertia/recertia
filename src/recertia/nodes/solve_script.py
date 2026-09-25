"""Shared script resolution and the two non-applicator script paths."""

from __future__ import annotations

import functools
from pathlib import Path

from contracts.failure import FailureSignal
from contracts.run import RunState
from recertia.memory.procedural.apply import script_from_skill
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


class ModelRequiredError(RuntimeError):
    """Scratch solving was selected but no model client is configured."""


def fill_skill_params(version, bound: dict) -> dict:
    """Copy bound parameters and fill skill-declared defaults."""

    params = dict(bound)
    for parameter in version.parameters:
        if parameter.name not in params and parameter.default is not None:
            params[parameter.name] = parameter.default
    return params


def resolve_script(state: RunState, ctx: NodeContext) -> list[str]:
    if ctx.script is not None:
        return ctx.script
    if state.strategy in ("apply", "adapt") and state.chosen is not None and ctx.store is not None:
        version = ctx.store.get_version(state.chosen.skill_id, state.chosen.version)
        params = fill_skill_params(version, state.chosen.bound_parameters)
        from recertia.solver.apply import bind_parameters

        raw = script_from_skill(version)
        return [bind_parameters(cmd, params) for cmd in raw]
    if state.strategy == "scratch" and ctx.model is not None:
        # Legacy single-shot path (no tools/transcripts wired).
        response = ctx.model.complete(
            f"Propose a single shell command to: {state.task.request}\n"
            "Reply with only the command, no markdown."
        )
        return [response.text.strip().splitlines()[0]]
    if state.strategy == "scratch" and (ctx.tools is not None or ctx.transcripts is not None):
        raise ModelRequiredError(
            "scratch solving requires a model client; set RECERTIA_MODEL_PROVIDER "
            "and credentials (or --model provider:id), or pass an explicit script / "
            "RECERTIA_ALLOW_STUB_MODEL=1 for offline demos"
        )
    return ["true"]


def solve_script_via_tools(
    state: RunState, ctx: NodeContext, attempt_no: int, script: list[str]
) -> NodeOutcome:
    assert ctx.tools is not None
    writer = (
        TranscriptWriter(ctx.transcripts, ctx.run_id, attempt_no)
        if ctx.transcripts is not None
        else None
    )
    meter = AttemptMeter.open(state)
    for op_seq, command in enumerate(script):
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
                note=f"tool-call budget exhausted: {exhausted}",
                updates={"transcript_ref": writer.finalize() if writer else None},
            )

        window = RuntimeWindow(ctx)

        def _run(cmd: str = command, seq: int = op_seq, win: RuntimeWindow = window) -> dict:
            if writer:
                writer.event("tool", tool="shell", command=cmd)
            result = ctx.tools.invoke(  # type: ignore[union-attr]
                "shell", {"command": cmd}, workdir=ctx.workdir, step_id=f"script-{seq}"
            )
            return {
                "returncode": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "flaky": ctx.tools.is_flaky("shell") if ctx.tools else False,
                "usage": win.delta().as_dict(),
            }

        result = ctx.op_once(op_seq, _run)
        meter.charge_delta(UsageDelta.from_dict(result.get("usage") or {}))
        record_new_affordances(ctx, window)
        if result["returncode"] != 0:
            detail = f"step {op_seq} exited {result['returncode']}: {command}"
            if result.get("flaky"):
                detail = f"flaky tool=shell: {detail}"
            return failed(
                state,
                meter,
                signal=FailureSignal(source="solver", detail=detail, at=now()),
                attempt_no=attempt_no,
                note=f"solver-raised failure signal at step {op_seq}",
                updates={"transcript_ref": writer.finalize() if writer else None},
            )

    return completed(
        state,
        meter,
        attempt_no=attempt_no,
        transcript_ref=writer.finalize() if writer else f"{ctx.run_id}/attempt-{attempt_no}",
        description="scripted attempt transcript",
    )


def solve_legacy_script(
    state: RunState, ctx: NodeContext, attempt_no: int, script: list[str]
) -> NodeOutcome:
    meter = AttemptMeter.open(state)
    for op_seq, command in enumerate(script):
        result = ctx.op_once(op_seq, functools.partial(run_command, command, ctx))
        # One legacy command is one charge whether it ran now or replayed from the ledger.
        meter.charge(tool_calls=1)
        if result["returncode"] != 0:
            return failed(
                state,
                meter,
                signal=FailureSignal(
                    source="solver",
                    detail=f"step {op_seq} exited {result['returncode']}: {command}",
                    at=now(),
                ),
                attempt_no=attempt_no,
                note=f"solver-raised failure signal at step {op_seq}",
            )

    return completed(
        state,
        meter,
        attempt_no=attempt_no,
        transcript_ref=f"{ctx.run_id}/attempt-{attempt_no}",
        description="scripted attempt transcript",
    )


def run_command(command: str, ctx: NodeContext) -> dict:
    return run_container_command(command, ctx.workdir)


def run_container_command(command: str, workdir: Path) -> dict:
    """Execute solver commands only through the approved OCI sandbox."""

    from recertia.solver.container import run_configured_command
    from recertia.solver.sandbox import SandboxError

    try:
        proc = run_configured_command(command, workdir=workdir, timeout_s=60)
    except SandboxError as exc:
        return {"returncode": 126, "stdout": "", "stderr": str(exc)}
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }
