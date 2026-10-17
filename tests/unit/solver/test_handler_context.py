"""HandlerContext is passed only when a handler annotates it; Path stays the shim."""

from __future__ import annotations

from pathlib import Path

from recertia.ports.execution import CommandRequest, CommandResult
from recertia.solver.handlers import shell_handler
from recertia.solver.registry import Tool, ToolRegistry, ToolResult
from recertia.solver.runtime import HandlerContext, ToolRuntime


class _RecordingExecutor:
    def __init__(self) -> None:
        self.requests: list[CommandRequest] = []

    def run(self, request: CommandRequest) -> CommandResult:
        self.requests.append(request)
        return CommandResult(returncode=0, stdout="ran", stderr="")


def test_runtime_passes_handler_context_when_annotated(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def marked(inputs: dict, ctx: HandlerContext) -> ToolResult:
        del inputs
        seen["kind"] = type(ctx).__name__
        seen["workdir"] = ctx.workdir
        seen["limits"] = ctx.limits
        return ToolResult(tool="marked", ok=True)

    registry = ToolRegistry()
    registry.register(Tool(name="marked", side_effect="read"), marked)
    runtime = ToolRuntime(registry, require_approval_for_non_read=False)
    runtime.invoke("marked", {}, workdir=tmp_path, step_id="s1")
    assert seen["kind"] == "HandlerContext"
    assert seen["workdir"] == tmp_path
    assert seen["limits"] is runtime.sandbox_limits


def test_runtime_keeps_path_for_unmigrated_handlers(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def legacy(inputs: dict, workdir: Path) -> ToolResult:
        del inputs
        seen["kind"] = type(workdir).__name__
        seen["workdir"] = workdir
        return ToolResult(tool="legacy", ok=True)

    registry = ToolRegistry()
    registry.register(Tool(name="legacy", side_effect="read"), legacy)
    runtime = ToolRuntime(registry, require_approval_for_non_read=False)
    runtime.invoke("legacy", {}, workdir=tmp_path, step_id="s1")
    assert seen["workdir"] == tmp_path
    assert seen["kind"] != "HandlerContext"


def test_shell_handler_uses_injected_executor(tmp_path: Path) -> None:
    executor = _RecordingExecutor()
    ctx = HandlerContext(
        workdir=tmp_path,
        executor=executor,  # type: ignore[arg-type]
        run_id="run-1",
        owner_id="owner-1",
        origin="tool",
    )
    result = shell_handler({"command": "echo hi"}, ctx)
    assert result.ok
    assert result.stdout == "ran"
    assert len(executor.requests) == 1
    request = executor.requests[0]
    assert request.command == "echo hi"
    assert request.workdir == tmp_path
    assert request.run_id == "run-1"
    assert request.owner_id == "owner-1"
    assert request.origin == "tool"


def test_shell_handler_still_accepts_a_path(tmp_path: Path) -> None:
    result = shell_handler({"command": "true"}, tmp_path)
    assert result.ok
    assert result.exit_code == 0
