"""SEC-5: CommandRequest.origin and model-shell policy on the local backend.

Full local confinement (shell=False, PR_SET_DUMPABLE, seccomp) is out of scope
for this slice; these tests lock the origin / argv gate that already ships.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.criteria import TaskCriterion
from contracts.run import RunState, Task
from recertia.graph.ops import OperationLedger
from recertia.ledger import HashChainLedger
from recertia.nodes.context import NodeContext
from recertia.nodes.solve_script import solve_script_via_tools
from recertia.ports.execution import CommandRequest, CommandResult, LocalExecutionCapability
from recertia.solver.command_policy import CommandPolicyError, enforce_model_shell
from recertia.solver.executor import LocalExecutor
from recertia.solver.handlers import agent_subtask_handler, shell_handler
from recertia.solver.registry import ToolResult
from recertia.solver.runtime import HandlerContext
from recertia.validation.criteria import CriterionRunner
from recertia.workspace import WorkspaceManager


class _RecordingExecutor:
    def __init__(self) -> None:
        self.requests: list[CommandRequest] = []

    def run(self, request: CommandRequest) -> CommandResult:
        self.requests.append(request)
        return CommandResult(returncode=0, stdout=request.command, stderr="")


def _local_config(**overrides: object):
    from recertia.ports.execution import ExecutionConfig

    base: dict[str, object] = dict(
        backend="local",
        runtime=None,
        image="python:3.12-slim",
        allow_custom_image=False,
        workdir_world_write=False,
        api_allow_local_exec=True,
        local_home_passthrough=False,
    )
    base.update(overrides)
    return ExecutionConfig(**base)  # type: ignore[arg-type]


def test_model_origin_refused_on_local_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "local")
    monkeypatch.setenv("RECERTIA_COMMAND_POLICY", "on")
    with pytest.raises(CommandPolicyError, match="container"):
        enforce_model_shell("echo ok")


def test_model_origin_allowed_on_container_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "container")
    monkeypatch.setenv("RECERTIA_COMMAND_POLICY", "on")
    assert enforce_model_shell("echo ok") == "echo ok"


def test_shell_handler_refuses_model_on_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "local")
    monkeypatch.setenv("RECERTIA_COMMAND_POLICY", "on")
    refused = shell_handler(
        {"command": "touch MARKER"},
        HandlerContext(workdir=tmp_path, origin="model"),
    )
    assert not refused.ok
    assert refused.exit_code == 126
    assert "container" in refused.stderr
    assert not (tmp_path / "MARKER").exists()


def test_shell_handler_allows_model_on_container_with_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "container")
    monkeypatch.setenv("RECERTIA_COMMAND_POLICY", "on")
    executor = _RecordingExecutor()
    allowed = shell_handler(
        {"command": "echo ok"},
        HandlerContext(workdir=tmp_path, origin="model", executor=executor),  # type: ignore[arg-type]
    )
    assert allowed.ok
    assert len(executor.requests) == 1
    assert executor.requests[0].origin == "model"
    assert executor.requests[0].command == "echo ok"


def test_shell_handler_refuses_model_command_not_on_argv_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "container")
    monkeypatch.setenv("RECERTIA_COMMAND_POLICY", "on")
    refused = shell_handler(
        {"command": "curl http://evil.example"},
        HandlerContext(workdir=tmp_path, origin="model"),
    )
    assert not refused.ok
    assert "allowlist" in refused.stderr or "forbidden" in refused.stderr


def test_argv_allowlist_rejects_python_c(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "container")
    monkeypatch.setenv("RECERTIA_COMMAND_POLICY", "on")
    with pytest.raises(CommandPolicyError, match="python -c"):
        enforce_model_shell("python -c 'print(1)'")


def test_local_executor_refuses_model_and_skill_origin(tmp_path: Path) -> None:
    executor = LocalExecutor(_local_config(), LocalExecutionCapability(purpose="pytest"))
    from recertia.solver.sandbox import SandboxError

    with pytest.raises(SandboxError, match="container"):
        executor.run(
            CommandRequest(command="echo bypass", workdir=tmp_path, origin="model")
        )
    with pytest.raises(SandboxError, match="container"):
        executor.run(
            CommandRequest(command="echo bypass", workdir=tmp_path, origin="skill")
        )


def test_criterion_runner_sets_command_request_origin(tmp_path: Path) -> None:
    executor = _RecordingExecutor()
    criterion = TaskCriterion(id="c1", kind="command", run="echo hi", source="caller")
    result = CriterionRunner(executor).score(criterion, workdir=tmp_path)  # type: ignore[arg-type]
    assert result.passed
    assert len(executor.requests) == 1
    assert executor.requests[0].origin == "criterion"
    assert executor.requests[0].command == "echo hi"


def test_solve_script_sets_invoke_origin_script(tmp_path: Path) -> None:
    """Unit-level: solve_script_via_tools stamps origin=script on tools.invoke."""

    class _RecordingTools:
        def __init__(self) -> None:
            self.origins: list[str] = []
            self.invocations: list[ToolResult] = []
            self.scheduler = type("S", (), {"conflicts": []})()

        def invoke(self, tool_name: str, inputs: dict, **kwargs: object) -> ToolResult:
            del tool_name, inputs
            self.origins.append(str(kwargs.get("origin", "")))
            result = ToolResult(tool="shell", ok=True, exit_code=0)
            self.invocations.append(result)
            return result

        def is_flaky(self, tool_name: str) -> bool:
            del tool_name
            return False

    tools = _RecordingTools()
    workdir = tmp_path / "work"
    workdir.mkdir()
    state = RunState(
        run_id="script-origin",
        task=Task(
            task_id="t",
            request="run script",
            submitted_at=datetime.now(timezone.utc),
        ),
        strategy="scratch",
    )
    ctx = NodeContext(
        run_id="script-origin",
        attempt_no=0,
        node="solve",
        workdir=workdir,
        workspaces=WorkspaceManager(tmp_path / "snapshots"),
        ledger=HashChainLedger(tmp_path / "ledger.jsonl"),
        ops=OperationLedger(tmp_path / "ops.db"),
        tools=tools,  # type: ignore[arg-type]
    )
    outcome = solve_script_via_tools(state, ctx, attempt_no=0, script=["echo one"])
    assert outcome.route == "attempt_ok" or outcome.state is not None
    assert tools.origins == ["script"]


def test_agent_subtask_stamps_origin_model_on_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "container")
    monkeypatch.setenv("RECERTIA_COMMAND_POLICY", "on")
    from recertia.solver.model import StubModelClient

    executor = _RecordingExecutor()
    result = agent_subtask_handler(
        {"intent": "touch MARKER"},
        HandlerContext(
            workdir=tmp_path,
            origin="tool",  # invoke-site default; handler must override
            executor=executor,  # type: ignore[arg-type]
            model=StubModelClient(responses=["echo repaired"]),
        ),
    )
    assert result.ok
    assert len(executor.requests) == 1
    assert executor.requests[0].origin == "model"
    assert executor.requests[0].command == "echo repaired"
