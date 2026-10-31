"""``/v1`` run routes answer busy leases and workspaces with their own retryable codes.

Each case holds a real lock (``RunLease``, ``WorkspaceLock``) or a real queue row; no
test makes a service raise by patching it. Every body is checked for leaks: no
``details`` key, a fixed message, and neither the workdir, the test root nor another
tenant's ids anywhere in ``response.text``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from recertia.api import create_app
from recertia.cli.exit import exit_code_for
from recertia.errors import Busy
from recertia.graph.lease import RunBusy, RunLease
from recertia.persistence.layout import RuntimeLayout
from recertia.runtime.errors import WorkspaceBusy
from recertia.runtime.workspace_lock import WorkspaceLock
from recertia.workers.queue import QueueItem
from tests.support.idempotency import close_app

_SRC = Path(__file__).resolve().parents[2] / "src"
_ROUTE_LOGGER = "recertia.api.routes.runs"
posix_flock = pytest.mark.skipif(sys.platform == "win32", reason="the workspace flock is a no-op on Windows")


def _app(root: Path, *tenants: str) -> tuple[Any, dict[str, dict[str, str]]]:
    app = create_app(root=root)
    headers = {}
    for tenant in tenants:
        issued = app.state.api_keys.issue(tenant_id=tenant, scopes={"runs", "exec"}, actor="test")
        headers[tenant] = {"X-API-Key": issued.secret}
    return app, headers


def _workdir(root: Path, tenant: str, run_id: str) -> Path:
    return (root / "workspaces" / tenant / run_id).resolve()


def _locks(root: Path) -> WorkspaceLock:
    return WorkspaceLock(RuntimeLayout(root).locks_dir)


def _create_body(run_id: str) -> dict[str, Any]:
    return {"request": "write output.txt", "run_id": run_id, "budget": {"max_attempts": 1}}


def _local_run_body(run_id: str) -> dict[str, Any]:
    return {
        "request": "resume me",
        "run_id": run_id,
        "script": ["true"],
        "budget": {"max_attempts": 1},
        "criteria": [{"id": "ok", "kind": "command", "run": "true", "source": "caller", "weight": 1.0}],
    }


def _assert_busy(response: Any, *, code: str, run_id: str, absent: tuple[str, ...]) -> None:
    assert response.status_code == 409, response.text
    assert response.json() == {"error": {"code": code, "message": code, "retryable": True, "run_id": run_id}}
    for text in absent:
        assert text not in response.text, text


@contextmanager
def _foreign_holder(root: Path, workdir: Path, owner_id: str) -> Iterator[None]:
    """Another process holds the workspace flock, as another tenant's worker would."""

    script = (
        "import sys\n"
        "from recertia.runtime.workspace_lock import WorkspaceLock\n"
        "handle = WorkspaceLock(sys.argv[1]).acquire(sys.argv[2], owner_id=sys.argv[3])\n"
        "print('held', flush=True)\n"
        "sys.stdin.readline()\n"
        "handle.release()\n"
    )
    env = {**os.environ, "PYTHONPATH": str(_SRC)}
    with subprocess.Popen(
        [sys.executable, "-c", script, str(RuntimeLayout(root).locks_dir), str(workdir), owner_id],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    ) as proc:
        assert proc.stdin is not None and proc.stdout is not None
        try:
            assert proc.stdout.readline().strip() == "held"
            yield
        finally:
            proc.stdin.write("\n")
            proc.stdin.flush()
            proc.wait(timeout=30)


def test_create_with_held_run_lease_is_run_busy(tmp_path: Path) -> None:
    root = tmp_path / "api-root"
    app, headers = _app(root, "t1")
    holder = RunLease(root / "runs" / "t1" / "locks" / "busy-lease.lock")
    holder.acquire()
    try:
        response = TestClient(app).post("/v1/runs", json=_create_body("busy-lease"), headers=headers["t1"])
    finally:
        holder.release()
    _assert_busy(
        response,
        code="run_busy",
        run_id="busy-lease",
        absent=(str(tmp_path), "busy-lease.lock", "details"),
    )


def test_create_with_held_workspace_is_workspace_busy(tmp_path: Path) -> None:
    root = tmp_path / "api-root"
    app, headers = _app(root, "t1")
    workdir = _workdir(root, "t1", "busy-ws")
    handle = _locks(root).acquire(workdir, owner_id="t1/holder-run")
    try:
        response = TestClient(app).post("/v1/runs", json=_create_body("busy-ws"), headers=headers["t1"])
    finally:
        handle.release()
    _assert_busy(
        response,
        code="workspace_busy",
        run_id="busy-ws",
        absent=(str(tmp_path), str(workdir), "holder-run", "details"),
    )


def test_sync_wait_workspace_busy_is_workspace_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The create releases its workspace hold; the worker then finds it taken.

    The hooks only order the steps: the embedded worker starts after a real lock is taken
    on the workdir, so the worker's own acquire fails and the run ends
    ``failed``/``workspace_busy``. The create's sync wait then answers 409.
    """

    root = tmp_path / "api-root"
    app, headers = _app(root, "t1")
    ctx = app.state.recertia
    workdir = _workdir(root, "t1", "sync-ws")
    start_worker = ctx.embedded.ensure_started
    real_wait = ctx.queue.wait
    seen: dict[str, Any] = {}

    def wait_holding_workspace(tenant_id: str, run_id: str, *, timeout_s: float) -> Any:
        handle = _locks(root).acquire(workdir, owner_id="t1/other-run")
        try:
            start_worker()
            finished = real_wait(tenant_id, run_id, timeout_s=timeout_s)
            seen["finished"] = finished
            return finished
        finally:
            handle.release()

    monkeypatch.setattr(ctx.embedded, "ensure_started", lambda: None)
    monkeypatch.setattr(ctx.queue, "wait", wait_holding_workspace)
    response = TestClient(app).post("/v1/runs", json=_create_body("sync-ws"), headers=headers["t1"])
    assert seen["finished"] is not None
    assert seen["finished"].status == "failed"
    assert seen["finished"].error_code == "workspace_busy"
    _assert_busy(
        response,
        code="workspace_busy",
        run_id="sync-ws",
        absent=(str(tmp_path), str(workdir), "other-run", "details"),
    )


@pytest.mark.parametrize("status", ["queued", "claimed"])
def test_resume_of_a_live_queue_row_is_run_busy(tmp_path: Path, status: str) -> None:
    root = tmp_path / "api-root"
    app, headers = _app(root, "t1")
    queue = app.state.recertia.queue
    app.state.recertia.embedded.stop()
    queue.enqueue(QueueItem(tenant_id="t1", run_id="live-row", kind="run", payload={}))
    if status == "claimed":
        claimed = queue.claim("test-holder")
        assert claimed
    assert queue.get("t1", "live-row").status == status
    response = TestClient(app).post("/v1/runs/live-row/resume", headers=headers["t1"])
    _assert_busy(response, code="run_busy", run_id="live-row", absent=(str(tmp_path), "details"))


def test_resume_with_held_workspace_is_workspace_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "local")
    monkeypatch.setenv("RECERTIA_API_ALLOW_LOCAL_EXEC", "1")
    root = tmp_path / "api-root"
    app, headers = _app(root, "t1")
    client = TestClient(app)
    created = client.post("/v1/runs", json=_local_run_body("resume-ws"), headers=headers["t1"])
    assert created.status_code == 200, created.text
    workdir = Path(created.json()["workdir"]).resolve()
    handle = _locks(root).acquire(workdir, owner_id="t1/holder-run")
    try:
        response = client.post("/v1/runs/resume-ws/resume", headers=headers["t1"])
    finally:
        handle.release()
    _assert_busy(
        response,
        code="workspace_busy",
        run_id="resume-ws",
        absent=(str(tmp_path), str(workdir), "holder-run", "details"),
    )


@posix_flock
def test_create_logs_busy_details_server_side_only(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    root = tmp_path / "api-root"
    app, headers = _app(root, "t1")
    workdir = _workdir(root, "t1", "logged-ws")
    with (
        _foreign_holder(root, workdir, "t1/holder-run"),
        caplog.at_level(logging.WARNING, logger=_ROUTE_LOGGER),
    ):
        response = TestClient(app).post("/v1/runs", json=_create_body("logged-ws"), headers=headers["t1"])
    _assert_busy(
        response,
        code="workspace_busy",
        run_id="logged-ws",
        absent=(str(workdir), "holder-run", "details"),
    )
    records = [r for r in caplog.records if r.name == _ROUTE_LOGGER]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    logged = records[0].getMessage()
    assert "workspace_busy" in logged
    assert str(workdir) in logged
    assert "t1/holder-run" in logged


@posix_flock
def test_foreign_tenant_holder_never_reaches_the_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tenant B holds the workspace that tenant A's create and resume resolve to."""

    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "local")
    monkeypatch.setenv("RECERTIA_API_ALLOW_LOCAL_EXEC", "1")
    root = tmp_path / "api-root"
    app, headers = _app(root, "tenant-a", "tenant-b")
    client = TestClient(app)
    tenant_b, run_b = "tenant-b", "run-b-secret"
    start_dir = _workdir(root, "tenant-a", "run-a")
    with _foreign_holder(root, start_dir, f"{tenant_b}/{run_b}"):
        started = client.post("/v1/runs", json=_create_body("run-a"), headers=headers["tenant-a"])
    _assert_busy(
        started,
        code="workspace_busy",
        run_id="run-a",
        absent=(tenant_b, run_b, str(start_dir), str(tmp_path), "details"),
    )

    created = client.post("/v1/runs", json=_local_run_body("run-a2"), headers=headers["tenant-a"])
    assert created.status_code == 200, created.text
    resume_dir = Path(created.json()["workdir"]).resolve()
    assert resume_dir == _workdir(root, "tenant-a", "run-a2")
    with _foreign_holder(root, resume_dir, f"{tenant_b}/{run_b}"):
        resumed = client.post("/v1/runs/run-a2/resume", headers=headers["tenant-a"])
    _assert_busy(
        resumed,
        code="workspace_busy",
        run_id="run-a2",
        absent=(tenant_b, run_b, str(resume_dir), str(tmp_path), "details"),
    )


def test_run_busy_from_the_thread_lock_names_no_path(tmp_path: Path) -> None:
    """Another thread of this process holds the run's lease, so the per-path lock is taken.

    ``/v1`` run bodies replace ``RunBusy``'s message with a fixed one, but a ``RunBusy``
    that escapes any other route reaches the app-level ``RecertiaError`` handler, which
    sends the message as it is. So the message itself must not carry the lock path.
    """

    lock_path = tmp_path / "runs" / "t1" / "locks" / "path-guard.lock"
    separators = ("/", "\\")  # either one, whatever the platform
    app = create_app(root=tmp_path / "api-root")

    @app.get("/escaped-run-busy")
    def _escaped_run_busy() -> None:
        RunLease(lock_path).acquire()

    holder = RunLease(lock_path)
    holder.acquire()
    try:
        with pytest.raises(RunBusy) as raised:
            RunLease(lock_path).acquire()
        response = TestClient(app).get("/escaped-run-busy")
    finally:
        holder.release()
        close_app(app)
    message = raised.value.message
    assert response.status_code == 409, response.text
    assert response.json() == {"error": {"code": "run_busy", "message": message, "retryable": True}}
    for text in (message, response.text):
        assert str(tmp_path) not in text, text
        for sep in separators:
            assert sep not in text, (sep, text)


@contextmanager
def _lease_row_held(tmp_path: Path) -> Iterator[Callable[[], object]]:
    """This run's ``run_leases`` row is held, with a live heartbeat, through another lock file.

    A new lease on its own lock file gets its thread lock and its flock, then finds the row
    held by an owner that has not expired: the row check raises.
    """

    db = tmp_path / "runs" / "runtime.db"
    locks = tmp_path / "runs" / "t1" / "locks"
    holder = RunLease(locks / "row-holder.lock", db_path=db, run_id="row-guard")
    holder.acquire()
    try:
        yield lambda: RunLease(locks / "row-guard.lock", db_path=db, run_id="row-guard").acquire()
    finally:
        holder.release()


@contextmanager
def _lease_flock_held(tmp_path: Path) -> Iterator[Callable[[], object]]:
    """Another process holds the lease's lock file, as a second server or CLI run would.

    A lease here gets this process's thread lock, then finds the file's flock taken.
    """

    lock_path = tmp_path / "runs" / "t1" / "locks" / "flock-guard.lock"
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from recertia.graph.lease import RunLease\n"
        "lease = RunLease(Path(sys.argv[1]))\n"
        "lease.acquire()\n"
        "print('held', flush=True)\n"
        "sys.stdin.readline()\n"
        "lease.release()\n"
    )
    env = {**os.environ, "PYTHONPATH": str(_SRC)}
    with subprocess.Popen(
        [sys.executable, "-c", script, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    ) as proc:
        assert proc.stdin is not None and proc.stdout is not None
        try:
            assert proc.stdout.readline().strip() == "held"
            yield lambda: RunLease(lock_path).acquire()
        finally:
            proc.stdin.write("\n")
            proc.stdin.flush()
            proc.wait(timeout=30)


@pytest.mark.parametrize(
    ("held", "raised_in"),
    [
        pytest.param(_lease_row_held, "_record_acquire", id="run-leases-row"),
        pytest.param(_lease_flock_held, "_flock_exclusive", id="flock", marks=posix_flock),
    ],
)
def test_run_busy_from_the_lease_row_and_the_flock_names_no_path(
    tmp_path: Path, held: Callable[[Path], Any], raised_in: str
) -> None:
    """The other two ``RunBusy`` raises in ``RunLease.acquire`` also name no path.

    As for the thread-lock raise above: a ``RunBusy`` that escapes a route other than run
    start and resume is sent with its own message, so neither the message nor the response
    may carry the lock path, the database path or the test root.
    """

    separators = ("/", "\\")  # either one, whatever the platform
    app = create_app(root=tmp_path / "api-root")
    try:
        with held(tmp_path) as attempt:

            @app.get("/escaped-run-busy")
            def _escaped_run_busy() -> None:
                attempt()

            with pytest.raises(RunBusy) as raised:
                attempt()
            response = TestClient(app).get("/escaped-run-busy")
    finally:
        close_app(app)
    assert raised.traceback[-1].name == raised_in, raised.traceback[-1]
    message = raised.value.message
    assert response.status_code == 409, response.text
    assert response.json() == {"error": {"code": "run_busy", "message": message, "retryable": True}}
    for text in (message, response.text):
        assert str(tmp_path) not in text, text
        for sep in separators:
            assert sep not in text, (sep, text)


def test_busy_errors_exit_six_on_the_cli() -> None:
    assert exit_code_for(RunBusy("x")) == 6
    assert exit_code_for(WorkspaceBusy("x")) == 6
    assert isinstance(RunBusy("x"), Busy)
    assert isinstance(RunBusy("x"), RuntimeError)
    assert RunBusy("x").code == "run_busy"


@posix_flock
def test_cli_side_workspace_busy_keeps_its_details(tmp_path: Path) -> None:
    root = tmp_path / "root"
    workdir = (tmp_path / "wd").resolve()
    workdir.mkdir()
    with _foreign_holder(root, workdir, "t1/holder-run"), pytest.raises(WorkspaceBusy) as raised:
        _locks(root).acquire(workdir, owner_id="t1/other")
    assert raised.value.details == {"workdir": str(workdir), "holder_owner_id": "t1/holder-run"}
    assert str(workdir) in raised.value.message
    err = raised.value.error_body()["error"]
    assert isinstance(err, dict)
    assert err["details"] == {"workdir": str(workdir), "holder_owner_id": "t1/holder-run"}


def test_in_process_workspace_busy_keeps_its_workdir(tmp_path: Path) -> None:
    root = tmp_path / "root"
    workdir = (tmp_path / "wd").resolve()
    handle = _locks(root).acquire(workdir, owner_id="t1/holder-run")
    try:
        with pytest.raises(WorkspaceBusy) as raised:
            _locks(root).acquire(workdir, owner_id="t1/other")
    finally:
        handle.release()
    assert raised.value.details == {"workdir": str(workdir)}
    assert str(workdir) in raised.value.message
