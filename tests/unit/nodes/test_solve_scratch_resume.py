"""F10: scratch observe-act records the model command inside op_once."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from contracts.run import RunState, Task
from recertia.graph.ops import OperationLedger
from recertia.ledger import HashChainLedger
from recertia.nodes.context import NodeContext
from recertia.nodes.solve_scratch import solve_scratch_observe_act
from recertia.solver.model import ModelResponse, StubModelClient
from recertia.solver.registry import ToolResult
from recertia.solver.transcript import TranscriptStore
from recertia.workspace import WorkspaceManager

NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


class _CountingModel(StubModelClient):
    def __init__(self) -> None:
        super().__init__(responses=["echo A", "echo B"], provider="stub", model_id="stub")
        self.complete_calls = 0

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        deadline_s: float | None = None,
    ) -> ModelResponse:
        self.complete_calls += 1
        return super().complete(prompt, system=system, deadline_s=deadline_s)


class _RecordingTools:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.invocations: list[ToolResult] = []
        self.scheduler = type("Sched", (), {"conflicts": []})()

    def invoke(
        self,
        tool_name: str,
        inputs: dict[str, Any],
        *,
        workdir: Path,
        step_id: str,
        origin: str = "tool",
    ) -> ToolResult:
        del workdir, step_id, origin
        command = str(inputs.get("command") or "")
        self.commands.append(command)
        result = ToolResult(
            tool=tool_name,
            ok=True,
            exit_code=0,
            stdout="A\n" if command.endswith("A") else "B\n",
        )
        self.invocations.append(result)
        return result


def _state() -> RunState:
    return RunState(
        run_id="scratch-resume",
        task=Task(task_id="t", request="print A", task_class="repo-chore", submitted_at=NOW),
        strategy="scratch",
    )


def _ctx(tmp_path: Path, ops: OperationLedger, model: _CountingModel, tools: _RecordingTools) -> NodeContext:
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    return NodeContext(
        run_id="scratch-resume",
        attempt_no=1,
        node="solve",
        workdir=workdir,
        workspaces=WorkspaceManager(tmp_path / "snaps"),
        ledger=HashChainLedger(tmp_path / "ledger.jsonl"),
        ops=ops,
        tools=tools,  # type: ignore[arg-type]
        model=model,
        transcripts=TranscriptStore(tmp_path / "transcripts"),
    )


def test_resume_replays_recorded_model_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "container")
    monkeypatch.setenv("RECERTIA_COMMAND_POLICY", "on")

    ops = OperationLedger(tmp_path / "ops.db")
    model = _CountingModel()
    tools = _RecordingTools()
    state = _state()

    first = solve_scratch_observe_act(state, _ctx(tmp_path, ops, model, tools), attempt_no=1)
    assert first.route == "attempt_completed"
    assert model.complete_calls == 1
    assert tools.commands == ["echo A"]

    applied, stored = ops.get("scratch-resume", 1, "solve", 0)
    assert applied is True
    assert isinstance(stored, dict)
    assert stored["command"] == "echo A"

    replayed = solve_scratch_observe_act(state, _ctx(tmp_path, ops, model, tools), attempt_no=1)
    assert replayed.route == "attempt_completed"
    assert model.complete_calls == 1
    assert tools.commands == ["echo A"]
    assert model._queue == ["echo B"]

    applied, stored = ops.get("scratch-resume", 1, "solve", 0)
    assert applied is True
    assert isinstance(stored, dict)
    assert stored["command"] == "echo A"
    assert stored["returncode"] == 0
