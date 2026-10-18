"""Run leases are exclusive, and the fencing token lands on checkpoints."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from contracts.criteria import TaskCriterion
from contracts.run import Task
from recertia.api import create_app
from recertia.graph.engine import GraphOrchestrator
from recertia.graph.lease import RunBusy, RunLease
from recertia.graph.ops import OperationLedger
from tests.support.http import error_text


def test_acquire_heartbeat_release_advances_token(tmp_path: Path) -> None:
    path = tmp_path / "locks" / "run-1.lock"
    first = RunLease(path)
    assert first.acquire() == 1
    assert first.incarnation == 1
    first.heartbeat()
    assert path.read_text(encoding="ascii").strip() == "1"
    first.release()

    second = RunLease(path)
    assert second.acquire() == 2
    assert second.incarnation == 1
    second.release()


def test_second_acquire_raises_run_busy(tmp_path: Path) -> None:
    path = tmp_path / "locks" / "run-1.lock"
    holder = RunLease(path)
    holder.acquire()
    try:
        with pytest.raises(RunBusy):
            RunLease(path).acquire()
    finally:
        holder.release()
    assert RunLease(path).acquire() == 2


def test_start_refuses_a_held_lease(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    holder = RunLease(runs / "locks" / "run-busy.lock")
    holder.acquire()
    orch = GraphOrchestrator(runs)
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    try:
        with pytest.raises(RunBusy):
            orch.start(
                "run-busy",
                Task(task_id="t1", request="pause", submitted_at=datetime.now(timezone.utc)),
                [
                    TaskCriterion(
                        id="output-exists",
                        kind="command",
                        run="test -f output.txt",
                        source="caller",
                        weight=1.0,
                    )
                ],
                workdir=workdir,
                max_steps=1,
            )
    finally:
        holder.release()
        orch.close()


def test_checkpoint_and_operation_store_fencing_token(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    orch = GraphOrchestrator(runs)
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    try:
        orch.start(
            "run-fenced",
            Task(task_id="t1", request="pause", submitted_at=datetime.now(timezone.utc)),
            [
                TaskCriterion(
                    id="output-exists",
                    kind="command",
                    run="test -f output.txt",
                    source="caller",
                    weight=1.0,
                )
            ],
            workdir=workdir,
            max_steps=1,
        )
        row = sqlite3.connect(runs / "checkpoints.db").execute(
            "SELECT fencing_token FROM checkpoints WHERE run_id=?",
            ("run-fenced",),
        ).fetchone()
        assert row is not None
        assert row[0] == 1
    finally:
        orch.close()

    ops = OperationLedger(tmp_path / "operations.db")
    try:
        assert ops.run_once("run-fenced", 1, "solve", 0, lambda: {"ok": True}, fencing_token=7) == {
            "ok": True
        }
        stored = sqlite3.connect(tmp_path / "operations.db").execute(
            "SELECT fencing_token FROM operations"
        ).fetchone()
        assert stored == (7,)
    finally:
        ops.close()


def test_create_run_returns_409_when_lease_is_held(tmp_path: Path) -> None:
    root = tmp_path / "api-root"
    app = create_app(root=root)
    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"runs", "exec"}, actor="test")
    holder = RunLease(root / "runs" / "t1" / "locks" / "api-busy.lock")
    holder.acquire()
    try:
        client = TestClient(app)
        created = client.post(
            "/v1/runs",
            json={
                "request": "write output.txt",
                "run_id": "api-busy",
                "budget": {"max_attempts": 1},
            },
            headers={"X-API-Key": issued.secret},
        )
    finally:
        holder.release()
    assert created.status_code == 409
    assert error_text(created) == "run_busy"
