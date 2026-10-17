"""A workspace has one holder (F11)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from contracts.criteria import TaskCriterion
from contracts.run import Task
from recertia.api import create_app
from recertia.graph.engine import GraphOrchestrator
from recertia.graph.lease import WorkspaceBusy, WorkspaceLock
from tests.support.http import error_text


def _task() -> Task:
    return Task(task_id="t1", request="pause", submitted_at=datetime.now(timezone.utc))


def _criteria() -> list[TaskCriterion]:
    return [
        TaskCriterion(
            id="output-exists",
            kind="command",
            run="test -f output.txt",
            source="caller",
            weight=1.0,
        )
    ]


def test_second_acquire_raises_workspace_busy(tmp_path: Path) -> None:
    workdir = tmp_path / "shared"
    workdir.mkdir()
    holder = WorkspaceLock(tmp_path / "runs", workdir, holder="run-a")
    holder.acquire()
    try:
        with pytest.raises(WorkspaceBusy) as caught:
            WorkspaceLock(tmp_path / "runs", workdir, holder="run-b").acquire()
        assert caught.value.holder == "run-a"
    finally:
        holder.release()
    again = WorkspaceLock(tmp_path / "runs", workdir, holder="run-b")
    again.acquire()
    again.release()


def test_start_refuses_a_held_workdir(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    holder = WorkspaceLock(runs, workdir, holder="run-a")
    holder.acquire()
    orch = GraphOrchestrator(runs)
    try:
        with pytest.raises(WorkspaceBusy):
            orch.start("run-b", _task(), _criteria(), workdir=workdir, max_steps=1)
    finally:
        holder.release()
        orch.close()


def test_second_run_same_workdir_is_workspace_busy(tmp_path: Path) -> None:
    root = tmp_path / "api-root"
    app = create_app(root=root)
    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"runs", "exec"}, actor="test")
    workdir = (root / "workspaces" / "t1" / "ws-busy").resolve()
    workdir.mkdir(parents=True)
    holder = WorkspaceLock(root / "runs" / "t1", workdir, holder="other")
    holder.acquire()
    try:
        client = TestClient(app)
        created = client.post(
            "/v1/runs",
            json={
                "request": "write output.txt",
                "run_id": "ws-busy",
                "budget": {"max_attempts": 1},
            },
            headers={"X-API-Key": issued.secret},
        )
    finally:
        holder.release()
    assert created.status_code == 409
    assert error_text(created) == "workspace_busy"
