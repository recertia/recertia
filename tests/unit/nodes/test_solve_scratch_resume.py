"""Scratch resume replays the recorded model command (F10)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.run import RunState, Task
from recertia.graph.ops import OperationLedger
from recertia.ledger import HashChainLedger
from recertia.nodes.context import NodeContext
from recertia.nodes.solve_scratch import solve_scratch_observe_act
from recertia.solver.model import StubModelClient
from recertia.solver.registry import ToolResult
from recertia.workspace import WorkspaceManager


class _Tools:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.invocations: list[ToolResult] = []
        self.scheduler = type("Scheduler", (), {"conflicts": []})()

    def invoke(
        self,
        name: str,
        inputs: dict[str, str],
        workdir: Path | None = None,
        step_id: str | None = None,
        origin: str | None = None,
    ) -> ToolResult:
        del name, workdir, step_id, origin
        self.commands.append(inputs["command"])
        result = ToolResult(tool="shell", ok=True, exit_code=0, stdout="ok")
        self.invocations.append(result)
        return result


def test_resume_replays_recorded_model_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "container")
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    model = StubModelClient(responses=["echo A"])
    tools = _Tools()
    state = RunState(
        run_id="scratch-run",
        task=Task(
            task_id="t",
            request="say hello",
            submitted_at=datetime.now(timezone.utc),
        ),
    )
    ctx = NodeContext(
        run_id="scratch-run",
        attempt_no=1,
        node="solve",
        workdir=workdir,
        workspaces=WorkspaceManager(tmp_path / "snapshots"),
        ledger=HashChainLedger(tmp_path / "ledger.jsonl"),
        ops=OperationLedger(tmp_path / "ops.db"),
        model=model,
        tools=tools,  # type: ignore[arg-type]
    )

    first = solve_scratch_observe_act(state, ctx, 1)
    assert "completed" in (first.note or "")
    assert tools.commands == ["echo A"]
    assert model.spend.calls == 1

    second = solve_scratch_observe_act(state, ctx, 1)
    assert tools.commands == ["echo A"]
    assert model.spend.calls == 1
    assert second.note == first.note
