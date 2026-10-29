"""``GET /v1/runs/{id}`` answers ``root_busy`` (503, retryable) while the runtime root is busy (#145)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from contracts.criteria import TaskCriterion
from contracts.run import RunManifest, RunState, Task
from recertia.api import create_app
from recertia.app.runs import RunService
from recertia.graph.persistence import open_run_persistence
from recertia.persistence.import_legacy import legacy_sqlite_pending
from recertia.persistence.layout import RuntimeLayout
from recertia.persistence.root_lock import RootBusy, RootLock


def _state(run_id: str) -> RunState:
    now = datetime.now(timezone.utc)
    return RunState(
        run_id=run_id,
        task=Task(task_id=run_id, request="x", task_class="repo-chore", submitted_at=now),
        manifest=RunManifest(index_snapshot_id="snap", criteria_hash="h"),
        criteria=[TaskCriterion(id="c", kind="command", run="true", source="caller")],
        criteria_locked_at=now,
    )


def _tenant_with_pending_import(root: Path, tenant: str, run_id: str) -> None:
    """``<root>/runs/<tenant>`` with leftover checkpoint files that are not imported yet."""

    persistence = open_run_persistence(root / "runs" / tenant, migrate="off")
    try:
        for seq in (1, 2):
            persistence.checkpoints.save(run_id, seq, f"n{seq}", None, _state(run_id))
    finally:
        persistence.close()
    assert legacy_sqlite_pending(RuntimeLayout(root).tenant(tenant))


def test_get_run_while_the_root_lock_is_held_is_root_busy_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "rt"
    _tenant_with_pending_import(root, "t1", "r")
    app = create_app(root=root)
    key = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test").secret
    client = TestClient(app, raise_server_exceptions=False)
    held = RootLock(RuntimeLayout(root).root_lock)
    held.try_exclusive()  # what a worker-shaped import, a backup or a restore holds
    try:
        resp = client.get("/v1/runs/r", headers={"X-API-Key": key})
        assert resp.status_code == 503, resp.text
        assert resp.headers["content-type"].startswith("application/json")
        error = resp.json()["error"]
        assert error["code"] == "root_busy", error
        assert error["retryable"] is True, error
        assert error["message"]
        assert "retry-after" not in resp.headers  # RootBusy sets no retry_after_s
        assert legacy_sqlite_pending(RuntimeLayout(root).tenant("t1"))  # nothing imported
    finally:
        held.release()
    ok = client.get("/v1/runs/r", headers={"X-API-Key": key})
    assert ok.status_code == 200, ok.text
    assert ok.json()["run_id"] == "r"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("create", "/v1/runs", {"request": "x", "mode": "async"}),
        ("resume", "/v1/runs/r/resume", None),
        ("cancel", "/v1/runs/r/cancel", None),
    ],
)
def test_run_route_mapper_turns_root_busy_into_root_busy_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    """Create, resume and cancel share GET's error mapper: a ``RootBusy`` from the service is
    ``root_busy``, 503, retryable, with no ``Retry-After``."""

    root = tmp_path / "rt"
    _tenant_with_pending_import(root, "t1", "r")  # cancel checks that the run exists first
    app = create_app(root=root)
    key = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test").secret
    client = TestClient(app, raise_server_exceptions=False)

    def _busy(*_args: object, **_kwargs: object) -> object:
        raise RootBusy("cannot open the runtime root while the root lock is held")

    monkeypatch.setattr(RunService, method, _busy)
    resp = client.post(path, json=body, headers={"X-API-Key": key})
    assert resp.status_code == 503, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    error = resp.json()["error"]
    assert error["code"] == "root_busy", error
    assert error["retryable"] is True, error
    assert error["message"]
    assert "retry-after" not in resp.headers  # RootBusy sets no retry_after_s
