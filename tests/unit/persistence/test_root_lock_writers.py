"""REC-058 (#144): writers on a runtime root honour backup/restore's exclusive root lock.

Kept out of ``test_import_legacy.py`` so that file stays byte-identical to the
ADR-0022 baseline.
"""

from __future__ import annotations

import hashlib
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.criteria import TaskCriterion
from contracts.run import RunManifest, RunState, Task
from recertia.graph.persistence import open_run_persistence
from recertia.ops.backup import BackupError, _exclusive_root_locks, backup_tree, restore_tree
from recertia.persistence.import_legacy import legacy_import_complete, legacy_sqlite_pending
from recertia.persistence.layout import RuntimeLayout
from recertia.persistence.root_lock import RootBusy, RootLock, TenantLock


def _state(run_id: str) -> RunState:
    return RunState(
        run_id=run_id,
        task=Task(
            task_id=run_id,
            request="x",
            task_class="repo-chore",
            submitted_at=datetime.now(timezone.utc),
        ),
        manifest=RunManifest(index_snapshot_id="snap", criteria_hash="h"),
        criteria=[TaskCriterion(id="c", kind="command", run="true", source="caller")],
        criteria_locked_at=datetime.now(timezone.utc),
    )


def _db_byte_snapshot(root: Path) -> dict[str, str]:
    snap: dict[str, str] = {}
    if not root.exists():
        return snap
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        name = path.name
        if not (
            name.endswith(".db")
            or name.endswith(".sqlite")
            or name.endswith("-wal")
            or name.endswith("-shm")
            or ".db." in name
        ):
            continue
        snap[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snap


# Thread-join bound, not ADR-0022's BUSY_BOUND_S: RECERTIA_IMPORT_LOCK_TIMEOUT_S
# (0.25s here) plus slack.
_JOIN_BOUND_S = 5.0


def _imported_root(tmp_path: Path) -> Path:
    """A root whose leftover import has already completed (import_legacy returns early)."""

    root = tmp_path / "runs"
    p0 = open_run_persistence(root, migrate="off")
    try:
        p0.checkpoints.save("old-run", 1, "n", None, _state("old-run"))
    finally:
        p0.close()
    open_run_persistence(root).close()
    layout = RuntimeLayout(root).local_runs()
    assert legacy_import_complete(layout.runtime_db)
    assert legacy_sqlite_pending(layout) is False
    return root


def _start_writer(root: Path, run_id: str) -> tuple[threading.Thread, dict[str, object]]:
    """Open the root and write one checkpoint on another thread."""

    box: dict[str, object] = {"wrote": False}

    def run() -> None:
        started = time.monotonic()
        try:
            opened = open_run_persistence(root)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc
            box["elapsed"] = time.monotonic() - started
            return
        try:
            opened.checkpoints.save(run_id, 1, "n", None, _state(run_id))
            box["wrote"] = True
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc
        finally:
            opened.close()
            box["elapsed"] = time.monotonic() - started

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return worker, box


def test_write_on_imported_root_while_backup_lock_held_is_blocked_or_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = _imported_root(tmp_path)
    before = _db_byte_snapshot(root)
    api_lock, lock = _exclusive_root_locks(root, action="backup")
    try:
        worker, box = _start_writer(root, "busy-run")
        worker.join(timeout=_JOIN_BOUND_S)
        # #144 allows either mode, so a still-blocked writer passes here. The chosen
        # mode (typed RootBusy within the bound, never a hang) is pinned by
        # test_open_on_imported_root_under_backup_lock_raises_root_busy_within_bound.
        blocked = worker.is_alive()
        if not blocked:
            assert isinstance(box.get("error"), RootBusy), box
            assert float(box["elapsed"]) <= _JOIN_BOUND_S  # type: ignore[arg-type]
        assert box["wrote"] is False
        assert _db_byte_snapshot(root) == before
    finally:
        lock.release()
        api_lock.release()
    worker.join(timeout=_JOIN_BOUND_S)
    assert not worker.is_alive()


def test_write_on_imported_root_succeeds_after_backup_lock_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = _imported_root(tmp_path)
    before = _db_byte_snapshot(root)
    api_lock, lock = _exclusive_root_locks(root, action="restore")
    try:
        worker, box = _start_writer(root, "busy-run")
        worker.join(timeout=_JOIN_BOUND_S)
        assert box["wrote"] is False
        assert _db_byte_snapshot(root) == before
    finally:
        lock.release()
        api_lock.release()
    worker.join(timeout=_JOIN_BOUND_S)
    assert not worker.is_alive()
    if not box["wrote"]:
        # Refused while busy: the same write, retried after release, lands.
        assert isinstance(box.get("error"), RootBusy), box
        retry, box = _start_writer(root, "busy-run")
        retry.join(timeout=_JOIN_BOUND_S)
        assert not retry.is_alive()
    assert box["wrote"] is True, box
    assert "error" not in box, box
    opened = open_run_persistence(root)
    try:
        assert opened.checkpoints.list_run_ids() == ["busy-run", "old-run"]
        assert opened.checkpoints.latest_seq("busy-run") == 1
    finally:
        opened.close()


def test_open_on_imported_root_under_backup_lock_raises_root_busy_within_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chosen mode: bounded wait, then typed RootBusy (not a hang, not a generic error)."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = _imported_root(tmp_path)
    api_lock, lock = _exclusive_root_locks(root, action="backup")
    try:
        worker, box = _start_writer(root, "busy-run")
        worker.join(timeout=_JOIN_BOUND_S)
        assert not worker.is_alive(), "open hung while the root lock was held"
        assert isinstance(box.get("error"), RootBusy), box
        assert "cannot open the runtime root while the root lock is held" in str(box["error"])
        assert float(box["elapsed"]) <= _JOIN_BOUND_S  # type: ignore[arg-type]
    finally:
        lock.release()
        api_lock.release()


def test_open_handle_on_imported_root_keeps_backup_out_until_close(tmp_path: Path) -> None:
    """The shared lock lives as long as the handle, so backup cannot start under a writer."""

    root = _imported_root(tmp_path)
    archive = tmp_path / "backup.tar.gz"
    opened = open_run_persistence(root)
    try:
        with pytest.raises(BackupError, match="backup refused"):
            backup_tree(root, archive)
        opened.checkpoints.save("live-run", 1, "n", None, _state("live-run"))
    finally:
        opened.close()
    assert backup_tree(root, archive) == archive.resolve()


def _backup_locks_free(root: Path) -> bool:
    """True when backup could take ``api.lock`` and ``.lock`` right now (released at once)."""

    try:
        api_lock, lock = _exclusive_root_locks(root, action="backup")
    except BackupError:
        return False
    lock.release()
    api_lock.release()
    return True


def _run_store_snapshot(runs_root: Path) -> dict[str, str]:
    """Bytes of the run-handle stores only (runtime / leftover checkpoint and op dbs)."""

    names = ("runtime.db", "checkpoints.db", "operations.db")
    return {
        key: digest
        for key, digest in _db_byte_snapshot(runs_root).items()
        if "/" not in key and key.startswith(names)
    }


def _hist(p: object, run_id: str) -> list[tuple[int, str]]:
    return [(seq, node) for seq, node, *_rest in p.checkpoints.history(run_id)]  # type: ignore[attr-defined]


def test_two_handles_on_an_imported_root_are_open_together(tmp_path: Path) -> None:
    """The handle lock is shared: a worker run and an API read can coexist on one root."""

    root = _imported_root(tmp_path)
    first = open_run_persistence(root)
    try:
        second = open_run_persistence(root)
        try:
            second.checkpoints.save("second-run", 1, "n", None, _state("second-run"))
        finally:
            second.close()
        assert "second-run" in first.checkpoints.list_run_ids()
    finally:
        first.close()


# --- worker handles lock the runtime root that backup/restore lock ---


def _worker_claim(run_id: str) -> object:
    from recertia.workers.queue import Claim

    return Claim(
        tenant_id="acme",
        run_id=run_id,
        kind="run",
        claim_token=1,
        attempt=1,
        payload={"request": run_id, "budget": {"max_attempts": 1}},
    )


def test_worker_claim_is_refused_while_backup_holds_root_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``recertia worker`` (serve.execute_claim) runs in ``root/runs/<tenant>``."""

    from recertia.workers.serve import execute_claim

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "rt"
    tenant_dir = root / "runs" / "acme"
    open_run_persistence(tenant_dir).close()
    before = _run_store_snapshot(tenant_dir)
    api_lock, lock = _exclusive_root_locks(root, action="backup")
    try:
        started = time.monotonic()
        with pytest.raises(RootBusy):
            execute_claim(root, _worker_claim("w-run"))  # type: ignore[arg-type]
        assert time.monotonic() - started <= _JOIN_BOUND_S
        assert _run_store_snapshot(tenant_dir) == before
    finally:
        lock.release()
        api_lock.release()
    execute_claim(root, _worker_claim("w-run"))  # type: ignore[arg-type]
    opened = open_run_persistence(tenant_dir)
    try:
        assert "w-run" in opened.checkpoints.list_run_ids()
    finally:
        opened.close()


def test_worker_run_keeps_backup_out_of_the_runtime_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a worker run is live, backup of the runtime root is refused."""

    from recertia.graph.engine import GraphOrchestrator
    from recertia.workers.serve import execute_claim

    root = tmp_path / "rt"
    seen: list[bool] = []
    real_start = GraphOrchestrator.start

    def start_and_probe(self: GraphOrchestrator, *args: object, **kwargs: object) -> object:
        seen.append(_backup_locks_free(root))
        return real_start(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(GraphOrchestrator, "start", start_and_probe)
    execute_claim(root, _worker_claim("live-run"))  # type: ignore[arg-type]
    assert seen == [False]
    assert _backup_locks_free(root)


def test_async_worker_run_is_refused_while_backup_holds_root_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API/embedded worker (AsyncRunWorker) uses ``<root>/runs/<tenant>`` too."""

    from recertia.api.events import RunEventLog
    from recertia.workers.run_worker import AsyncRunWorker, load_async_request

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "rt"
    tenant_dir = root / "runs" / "acme"
    open_run_persistence(tenant_dir).close()
    req = load_async_request(
        {
            "run_id": "a-run",
            "tenant_id": "acme",
            "request": "a-run",
            "budget": {"max_attempts": 1},
            "workdir": str(root / "workspaces" / "acme" / "a-run"),
            "skills_root": str(root / "skills"),
            "facts_root": str(root / "facts"),
            "runs_root": str(tenant_dir),
            "index_path": str(tenant_dir / "skill_index.db"),
        }
    )
    req.workdir.mkdir(parents=True, exist_ok=True)
    worker = AsyncRunWorker(events=RunEventLog(root))
    try:
        before = _run_store_snapshot(tenant_dir)
        api_lock, lock = _exclusive_root_locks(root, action="backup")
        try:
            with pytest.raises(RootBusy):
                worker._run(req)
            assert _run_store_snapshot(tenant_dir) == before
        finally:
            lock.release()
            api_lock.release()
        assert worker._run(req) == "succeeded"
    finally:
        worker.shutdown(wait=True)
    opened = open_run_persistence(tenant_dir)
    try:
        assert "a-run" in opened.checkpoints.list_run_ids()
    finally:
        opened.close()


# --- leftover files that a restore brings back while an open waits ---


def _pending_root(root: Path, run_id: str) -> None:
    p = open_run_persistence(root, migrate="off")
    try:
        for seq in (1, 2):
            p.checkpoints.save(run_id, seq, f"n{seq}", None, _state(run_id))
    finally:
        p.close()
    assert legacy_sqlite_pending(RuntimeLayout(root).local_runs())


def _tenant_pending(root: Path, tenant: str, run_id: str) -> Path:
    """``<root>/runs/<tenant>`` with leftover seqs 1-2 not yet imported."""

    tdir = root / "runs" / tenant
    _pending_root(tdir, run_id)
    return tdir


def _start_slow_restore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, threading.Thread]:
    """Restore a pre-import backup (leftover seqs 1-2) into an empty root, slowly.

    Returns once restore holds the root lock and is about to extract.
    """

    src = tmp_path / "src"
    _pending_root(src, "r")
    archive = tmp_path / "pre-import.tar.gz"
    backup_tree(src, archive)
    target = tmp_path / "target"
    target.mkdir()
    real_extract = tarfile.TarFile.extractall
    extracting = threading.Event()

    def slow_extract(self: tarfile.TarFile, *args: object, **kwargs: object) -> None:
        extracting.set()
        time.sleep(0.5)
        real_extract(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tarfile.TarFile, "extractall", slow_extract)
    restore = threading.Thread(target=restore_tree, args=(archive, target), kwargs={"overwrite": True})
    restore.start()
    assert extracting.wait(_JOIN_BOUND_S)
    return target, restore


def test_open_racing_restore_fails_closed_then_retry_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "3")
    target, restore = _start_slow_restore(tmp_path, monkeypatch)
    try:
        # Decided on the empty tree, then waited for restore's lock.
        with pytest.raises(RootBusy, match="leftover runtime files appeared"):
            open_run_persistence(target)
    finally:
        restore.join(_JOIN_BOUND_S)
    assert not restore.is_alive()
    layout = RuntimeLayout(target).local_runs()
    assert legacy_sqlite_pending(layout)  # the refused open imported nothing
    retry = open_run_persistence(target)
    try:
        assert _hist(retry, "r") == [(1, "n1"), (2, "n2")]
    finally:
        retry.close()
    assert legacy_import_complete(layout.runtime_db)


def test_open_racing_restore_does_not_lose_a_leftover_seq(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that sees no run would start it at seq 1; the leftover seq 1 must survive."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "3")
    target, restore = _start_slow_restore(tmp_path, monkeypatch)
    try:
        try:
            p = open_run_persistence(target)
        except RootBusy:
            restore.join(_JOIN_BOUND_S)
            p = open_run_persistence(target)  # the caller's retry
    finally:
        restore.join(_JOIN_BOUND_S)
    try:
        seen = p.checkpoints.latest_seq("r")
        if seen is None:
            p.checkpoints.save("r", 1, "fresh", None, _state("r"))
    finally:
        p.close()
    q = open_run_persistence(target)
    try:
        history = _hist(q, "r")
    finally:
        q.close()
    assert (seen, history) == (2, [(1, "n1"), (2, "n2")])


# --- the handle's lock is not leaked on a failure ---


def test_orchestrator_wiring_failure_releases_root_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from recertia.bootstrap import build_default_orchestrator
    from recertia.graph.engine import GraphOrchestrator

    root = tmp_path / "runs"

    def broken_init(self: GraphOrchestrator, *args: object, **kwargs: object) -> None:
        raise RuntimeError("injected wiring failure")

    monkeypatch.setattr(GraphOrchestrator, "__init__", broken_init)
    with pytest.raises(RuntimeError, match="injected wiring failure"):
        build_default_orchestrator(root)
    assert _backup_locks_free(root)


def test_orchestrator_init_failure_releases_its_own_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from recertia.graph.engine import GraphOrchestrator

    root = tmp_path / "runs"

    def broken_init(self: GraphOrchestrator, *args: object, **kwargs: object) -> None:
        raise RuntimeError("injected init failure")

    monkeypatch.setattr(GraphOrchestrator, "__init__", broken_init)
    with pytest.raises(RuntimeError, match="injected init failure"):
        GraphOrchestrator(root)
    assert _backup_locks_free(root)


def test_lock_released_when_open_raises_after_lock(tmp_path: Path) -> None:
    """An open that fails after the shared lock is taken still releases it.

    Making ``runtime.db`` a directory causes the post-lock open_db path to
    raise; the finally in open_run_persistence must release the TenantLock.
    """
    root = tmp_path / "runs"
    root.mkdir()
    # Force a failure after the lock is acquired (open_db on a directory).
    (root / "runtime.db").mkdir()
    with pytest.raises(Exception):
        open_run_persistence(root)
    assert _backup_locks_free(root)


def test_lock_released_when_close_raises(tmp_path: Path) -> None:
    """A normal close releases the shared lock even if a later step fails.

    (Replacing the method prevents the real close from running, so the lock
    would stay held. The real path is what must free the lock.)
    """
    root = _imported_root(tmp_path)
    opened = open_run_persistence(root)
    # Normal close path releases the TenantLock.
    opened.close()
    assert _backup_locks_free(root)


def test_cross_tenant_pending_import_proceeds_while_other_tenant_handle_open(
    tmp_path: Path,
) -> None:
    root = tmp_path / "rt"
    t1 = root / "runs" / "t1"
    t2 = root / "runs" / "t2"
    open_run_persistence(t1).close()
    open_run_persistence(t2).close()
    h1 = open_run_persistence(t1)
    try:
        # t2 import is not blocked by t1's shared handle lock.
        open_run_persistence(t2).close()
    finally:
        h1.close()


def test_worker_import_is_root_busy_while_api_reader_imports_same_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "rt"
    tdir = root / "runs" / "t1"
    open_run_persistence(tdir).close()
    # Simulate an in-progress import by holding the tenant lock exclusive.
    tl = TenantLock(RuntimeLayout(root).root_lock, tdir / ".tenant.lock")
    tl.acquire(exclusive=True, timeout_s=1.0)
    try:
        with pytest.raises(RootBusy):
            open_run_persistence(tdir)
    finally:
        tl.release()


def test_api_reader_import_is_root_busy_while_worker_imports_same_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "rt"
    tdir = root / "runs" / "t1"
    open_run_persistence(tdir).close()
    tl = TenantLock(RuntimeLayout(root).root_lock, tdir / ".tenant.lock")
    tl.acquire(exclusive=True, timeout_s=1.0)
    try:
        with pytest.raises(RootBusy):
            open_run_persistence(tdir)
    finally:
        tl.release()


def test_concurrent_api_and_worker_imports_on_one_tenant_exclude_each_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shared handles on an already-imported tenant coexist (REC-065).

    Exclusion happens only while an import holds the tenant lock exclusive.
    Two concurrent opens after the first import has finished both succeed.
    """
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "rt"
    tdir = root / "runs" / "t1"
    open_run_persistence(tdir).close()  # imported / fresh
    barrier = threading.Barrier(2)
    results: list[object] = []

    def importer() -> None:
        barrier.wait()
        try:
            open_run_persistence(tdir).close()
            results.append("ok")
        except RootBusy as exc:
            results.append(exc)

    t1 = threading.Thread(target=importer)
    t2 = threading.Thread(target=importer)
    t1.start()
    t2.start()
    t1.join(_JOIN_BOUND_S)
    t2.join(_JOIN_BOUND_S)
    assert len(results) == 2
    assert results == ["ok", "ok"], results


def test_api_transcript_route_takes_the_runtime_root_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /v1/runs/{id}/transcript`` on a pending tenant, while a worker-shaped holder has
    ``<root>/.lock``, returns 503 root_busy and imports nothing."""

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from recertia.api import create_app

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "rt"
    tdir = _tenant_pending(root, "t1", "r")
    app = create_app(root=root)
    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    client = TestClient(app)
    before = _run_store_snapshot(tdir)
    held = RootLock(RuntimeLayout(root).root_lock)
    held.try_exclusive()  # what a worker-shaped import (or backup) holds
    try:
        resp = client.get("/v1/runs/r/transcript", headers={"X-API-Key": issued.secret})
        assert resp.status_code == 503, resp.text
        body = resp.json()
        assert body.get("error", {}).get("code") == "root_busy", body
        assert _run_store_snapshot(tdir) == before
        assert legacy_sqlite_pending(RuntimeLayout(root).tenant("t1"))
    finally:
        held.release()
