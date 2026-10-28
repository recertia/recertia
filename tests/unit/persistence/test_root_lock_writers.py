"""REC-058 (#144): writers on a runtime root honour backup/restore's exclusive root lock.

Kept out of ``test_import_legacy.py`` so that file stays byte-identical to the
ADR-0022 baseline.
"""

from __future__ import annotations

import hashlib
import io
import os
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
from recertia.persistence.root_lock import RootBusy, RootLock, RootLockBusy, TenantLock


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
        build_default_orchestrator(root, skills_root=tmp_path / "skills", facts_root=tmp_path / "facts")
    assert _backup_locks_free(root)


def test_orchestrator_init_failure_releases_its_own_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import recertia.graph.engine as engine

    root = tmp_path / "runs"

    def broken_hooks() -> list[object]:
        raise RuntimeError("injected init failure")

    monkeypatch.setattr(engine, "default_hop_hooks", broken_hooks)
    with pytest.raises(RuntimeError, match="injected init failure"):
        engine.GraphOrchestrator(root)
    assert _backup_locks_free(root)


def test_lock_released_when_open_raises_after_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import recertia.graph.persistence as gp

    root = tmp_path / "runs"
    open_run_persistence(root).close()

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("injected")

    monkeypatch.setattr(gp, "TrajectoryStore", boom)
    with pytest.raises(OSError, match="injected"):
        open_run_persistence(root)
    monkeypatch.undo()
    assert _backup_locks_free(root)


def test_lock_released_when_close_raises(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    p = open_run_persistence(root)

    def boom() -> None:
        raise RuntimeError("injected close")

    p.checkpoints.close = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="injected close"):
        p.close()
    assert _backup_locks_free(root)


# --- REC-065 (#158) lifted the cross-tenant RootBusy ---


def test_cross_tenant_pending_import_proceeds_while_other_tenant_handle_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REC-058 pinned typed RootBusy here: one root lock for all tenants. Since REC-065
    each tenant has its own lock, so tenant-b imports while tenant-a's handle is open
    (timeout 0: any wait would be RootBusy), and the tenants still do not see each other."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0")
    rt = RuntimeLayout(tmp_path / "rt")
    la, lb = rt.tenant("tenant-a"), rt.tenant("tenant-b")
    pb0 = open_run_persistence(lb, migrate="off")
    try:
        for seq in (1, 2):
            pb0.checkpoints.save("b-run", seq, f"n{seq}", None, _state("b-run"))
    finally:
        pb0.close()
    assert legacy_sqlite_pending(lb)
    pa = open_run_persistence(la)
    try:
        pa.checkpoints.save("a-run", 1, "a1", None, _state("a-run"))
        pb = open_run_persistence(lb)  # imports tenant-b under tenant-a's live handle
        try:
            assert _hist(pb, "b-run") == [(1, "n1"), (2, "n2")]
            assert pb.checkpoints.list_run_ids() == ["b-run"]
        finally:
            pb.close()
        assert pa.checkpoints.list_run_ids() == ["a-run"]  # B never visible through A
    finally:
        pa.close()
    assert legacy_import_complete(lb.runtime_db)
    pb = open_run_persistence(lb)  # re-open: no duplicate rows, no re-import
    try:
        assert _hist(pb, "b-run") == [(1, "n1"), (2, "n2")]
    finally:
        pb.close()
    pa = open_run_persistence(la)
    try:
        assert pa.checkpoints.list_run_ids() == ["a-run"]
    finally:
        pa.close()


# --- API-process run handles lock <root>/.lock like workers ---


def _tenant_pending(root: Path, tenant: str, run_id: str) -> Path:
    """``<root>/runs/<tenant>`` with leftover seqs 1-2 not yet imported."""

    tdir = root / "runs" / tenant
    _pending_root(tdir, run_id)
    return tdir


def _gate_import(monkeypatch: pytest.MonkeyPatch, *, hold_s: float | None = None) -> dict[str, object]:
    """Wrap ``_import_under_lock``: count overlapping entries; the first one waits.

    With ``hold_s`` every entry sleeps that long. Otherwise the first entry waits on
    ``gate["release"]`` (bounded by ``_JOIN_BOUND_S``) after setting ``gate["inside"]``.
    """

    import recertia.persistence.import_legacy as il

    real = il._import_under_lock
    guard = threading.Lock()
    gate: dict[str, object] = {
        "inside": threading.Event(),
        "release": threading.Event(),
        "active": 0,
        "max_active": 0,
        "entries": 0,
    }

    def gated(layout: object) -> bool:
        with guard:
            gate["entries"] = int(gate["entries"]) + 1  # type: ignore[call-overload]
            first = gate["entries"] == 1
            gate["active"] = int(gate["active"]) + 1  # type: ignore[call-overload]
            gate["max_active"] = max(int(gate["max_active"]), int(gate["active"]))  # type: ignore[call-overload]
        try:
            if hold_s is not None:
                time.sleep(hold_s)
            elif first:
                gate["inside"].set()  # type: ignore[attr-defined]
                gate["release"].wait(_JOIN_BOUND_S)  # type: ignore[attr-defined]
            return real(layout)  # type: ignore[arg-type]
        finally:
            with guard:
                gate["active"] = int(gate["active"]) - 1  # type: ignore[call-overload]

    monkeypatch.setattr(il, "_import_under_lock", gated)
    return gate


def _in_thread(fn: object) -> tuple[threading.Thread, dict[str, object]]:
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["result"] = fn()  # type: ignore[operator]
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def _worker_shaped_open(root: Path, tdir: Path) -> list[tuple[int, str]]:
    p = open_run_persistence(tdir, runtime_root=root)
    try:
        return _hist(p, "r")
    finally:
        p.close()


def _assert_imported_once(root: Path, tdir: Path) -> None:
    layout = RuntimeLayout(root).tenant(tdir.name)
    assert legacy_import_complete(layout.runtime_db)
    assert _worker_shaped_open(root, tdir) == [(1, "n1"), (2, "n2")]


def test_worker_import_is_root_busy_while_api_reader_imports_same_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API ``_load_from_checkpoints`` holds the import lock; a worker open must wait it out."""

    from recertia.api import _load_from_checkpoints

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.3")
    root = tmp_path / "rt"
    tdir = _tenant_pending(root, "acme", "r")
    gate = _gate_import(monkeypatch)
    api, box = _in_thread(lambda: _load_from_checkpoints(root, "acme", "r"))
    try:
        assert gate["inside"].wait(_JOIN_BOUND_S)  # type: ignore[attr-defined]
        with pytest.raises(RootBusy):
            _worker_shaped_open(root, tdir)
    finally:
        gate["release"].set()  # type: ignore[attr-defined]
        api.join(_JOIN_BOUND_S)
    assert not api.is_alive()
    assert "error" not in box, box
    assert getattr(box["result"], "run_id", None) == "r"
    assert gate["max_active"] == 1
    _assert_imported_once(root, tdir)


def test_api_reader_import_is_root_busy_while_worker_imports_same_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reverse: a worker import holds the lock; the API's open raises typed RootBusy."""

    from recertia.api import _load_from_checkpoints

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.3")
    root = tmp_path / "rt"
    tdir = _tenant_pending(root, "acme", "r")
    gate = _gate_import(monkeypatch)
    worker, box = _in_thread(lambda: _worker_shaped_open(root, tdir))
    try:
        assert gate["inside"].wait(_JOIN_BOUND_S)  # type: ignore[attr-defined]
        with pytest.raises(RootBusy):
            _load_from_checkpoints(root, "acme", "r")
    finally:
        gate["release"].set()  # type: ignore[attr-defined]
        worker.join(_JOIN_BOUND_S)
    assert not worker.is_alive()
    assert box.get("result") == [(1, "n1"), (2, "n2")], box
    assert gate["max_active"] == 1
    _assert_imported_once(root, tdir)


def test_concurrent_api_and_worker_imports_on_one_tenant_exclude_each_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both start at once. Before the fix both entered the import, and one could raise
    ``sqlite3.IntegrityError`` (schema_migrations.version). Now one imports and the
    other gets typed ``RootBusy`` or finds the import done."""

    from recertia.api import _load_from_checkpoints

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.2")
    root = tmp_path / "rt"
    tdir = _tenant_pending(root, "acme", "r")
    gate = _gate_import(monkeypatch, hold_s=0.6)
    start = threading.Barrier(2)

    def after_barrier(fn: object) -> object:
        start.wait(_JOIN_BOUND_S)
        return fn()  # type: ignore[operator]

    api, api_box = _in_thread(lambda: after_barrier(lambda: _load_from_checkpoints(root, "acme", "r")))
    worker, worker_box = _in_thread(lambda: after_barrier(lambda: _worker_shaped_open(root, tdir)))
    for thread in (api, worker):
        thread.join(_JOIN_BOUND_S * 2)
        assert not thread.is_alive()
    boxes = {"api": api_box, "worker": worker_box}
    errors = {name: box["error"] for name, box in boxes.items() if "error" in box}
    assert all(isinstance(exc, RootBusy) for exc in errors.values()), errors
    assert len(errors) < 2, errors  # one of them imported
    assert gate["max_active"] == 1, gate
    assert gate["entries"] == 1, gate
    _assert_imported_once(root, tdir)


def test_api_transcript_route_takes_the_runtime_root_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /v1/runs/{id}/transcript`` on a pending tenant, while a worker-shaped holder has
    ``<root>/.lock``, raises typed RootBusy and imports nothing."""

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from recertia.api import create_app
    from recertia.persistence.root_lock import RootLock

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
        with pytest.raises(RootBusy):
            client.get("/v1/runs/r/transcript", headers={"X-API-Key": issued.secret})
        assert _run_store_snapshot(tdir) == before
        assert legacy_sqlite_pending(RuntimeLayout(root).tenant("t1"))
    finally:
        held.release()
    ok = client.get("/v1/runs/r/transcript", headers={"X-API-Key": issued.secret})
    assert ok.status_code == 200, ok.text
    _assert_imported_once(root, tdir)


# --- migrate=off re-checks its decision under the lock ---


def _start_slow_restore_of(archive: Path, target: Path, monkeypatch: pytest.MonkeyPatch) -> threading.Thread:
    """Restore ``archive`` over ``target`` slowly; return once restore holds the lock."""

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
    return restore


@pytest.mark.parametrize("target_kind", ["empty", "pending"])
def test_migrate_off_open_racing_post_import_restore_does_not_lose_a_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_kind: str
) -> None:
    """An off-mode open decides "leftover files", then waits while a restore brings back
    an imported runtime.db. Before the fix it wrote seq 3 to the leftover checkpoints.db,
    which every later open ignores, so seq 3 was lost."""

    # The restore holds the root lock for at least 0.5 s (the slow extract), the open waits
    # at most 0.25 s, so the open is always refused with the "root lock is held" message.
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    src = tmp_path / "src"
    _pending_root(src, "r")
    open_run_persistence(src).close()  # imported; leftover files kept for rollback
    assert legacy_import_complete(RuntimeLayout(src).local_runs().runtime_db)
    archive = tmp_path / "post-import.tar.gz"
    backup_tree(src, archive)
    target = tmp_path / "target"
    if target_kind == "pending":
        _pending_root(target, "other")
    else:
        target.mkdir()
    restore = _start_slow_restore_of(archive, target, monkeypatch)
    try:
        try:
            p = open_run_persistence(target, migrate="off")  # decided before the restore
        except RootBusy as exc:
            assert "cannot open the runtime root while the root lock is held" in str(exc), exc
            restore.join(_JOIN_BOUND_S)
            p = open_run_persistence(target, migrate="off")  # the caller's retry
    finally:
        restore.join(_JOIN_BOUND_S)
    assert not restore.is_alive()
    try:
        p.checkpoints.save("r", 3, "n3", None, _state("r"))
    finally:
        p.close()
    for mode in ("off", "auto"):
        q = open_run_persistence(target, migrate=mode)  # type: ignore[arg-type]
        try:
            assert _hist(q, "r") == [(1, "n1"), (2, "n2"), (3, "n3")], mode
        finally:
            q.close()


def test_migrate_off_open_refused_by_recheck_leaves_the_lock_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-check releases the shared lock it took before raising."""

    import recertia.graph.persistence as gp

    root = tmp_path / "runs"
    _pending_root(root, "r")
    real = gp._resolve_runtime_db
    calls: list[str] = []

    def flip(layout: object, mode: str) -> tuple[bool, Path | None]:
        calls.append(mode)
        if len(calls) == 1:
            return False, None  # the pre-lock decision: leftover files
        return True, None  # under the lock: the root is now imported

    monkeypatch.setattr(gp, "_resolve_runtime_db", flip)
    with pytest.raises(RootBusy, match="imported while waiting"):
        open_run_persistence(root, migrate="off")
    monkeypatch.setattr(gp, "_resolve_runtime_db", real)
    assert calls == ["off", "off"]
    assert _backup_locks_free(root)


# --- a RootBusy claim is re-queued, not failed ---


def _enqueue(root: Path, tenant: str, run_id: str, payload: dict[str, object]) -> None:
    from recertia.workers.queue import QueueItem, open_run_queue

    queue = open_run_queue(root)
    try:
        queue.enqueue(QueueItem(tenant_id=tenant, run_id=run_id, kind="run", payload=payload))
    finally:
        queue.close()


def _queue_row(root: Path, tenant: str, run_id: str) -> object:
    from recertia.workers.queue import open_run_queue

    queue = open_run_queue(root)
    try:
        return queue.get(tenant, run_id)
    finally:
        queue.close()


def _queue_due(root: Path, tenant: str, run_id: str) -> str:
    import sqlite3

    conn = sqlite3.connect(RuntimeLayout(root).control_db)
    try:
        row = conn.execute(
            "SELECT not_before FROM run_queue WHERE tenant_id=? AND run_id=?", (tenant, run_id)
        ).fetchone()
    finally:
        conn.close()
    return str(row[0])


def _hold_root(root: Path, holder: str) -> object:
    """Something else holds a lock tenant-b needs, or (``other_tenant_handle``) does not.

    ``same_tenant_import``: what an import of tenant-b holds (``<root>/.lock`` shared,
    tenant-b's tenant lock exclusive). ``backup``: ``api.lock`` and ``<root>/.lock``
    exclusive. ``other_tenant_handle``: a live tenant-a handle (REC-065: no conflict).
    """

    if holder == "other_tenant_handle":
        p = open_run_persistence(root / "runs" / "tenant-a", runtime_root=root)
        return p.close
    if holder == "same_tenant_import":
        held = TenantLock(RuntimeLayout(root).root_lock, RuntimeLayout(root).tenant("tenant-b").tenant_lock)
        held.acquire(exclusive=True, timeout_s=0)
        return held.release
    api_lock, lock = _exclusive_root_locks(root, action="backup")

    def release() -> None:
        lock.release()
        api_lock.release()

    return release


@pytest.mark.parametrize("holder", ["same_tenant_import", "backup"])
def test_queue_worker_requeues_a_root_busy_claim_without_spending_an_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, holder: str
) -> None:
    """``recertia worker`` (serve.run_once): RootBusy is retryable.

    Tenant-b has a pending leftover import. With ``same_tenant_import`` another import
    of tenant-b holds its tenant lock, so this one is refused. (Until REC-065 the holder
    here was a tenant-a handle; another tenant no longer refuses the claim, see
    ``test_queue_worker_claim_runs_while_another_tenant_handle_is_open``.)
    """

    import recertia.workers.serve as serve

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "rt"
    tdir = _tenant_pending(root, "tenant-b", "b-old")
    _enqueue(root, "tenant-b", "b-new", {"request": "b-new", "budget": {"max_attempts": 1}})
    before = _run_store_snapshot(tdir)
    release = _hold_root(root, holder)
    try:
        started = datetime.now(timezone.utc).isoformat()
        assert serve.run_once(root, worker="w1") == 0
        row = _queue_row(root, "tenant-b", "b-new")
        assert (row.status, row.attempt, row.error_code) == ("queued", 0, "root_busy")  # type: ignore[attr-defined]
        assert _queue_due(root, "tenant-b", "b-new") > started  # backed off
        assert serve.run_once(root, worker="w1") == 0  # not due yet: nothing claimed
        assert _run_store_snapshot(tdir) == before
        assert legacy_sqlite_pending(RuntimeLayout(root).tenant("tenant-b"))
    finally:
        release()  # type: ignore[operator]
    import sqlite3

    conn = sqlite3.connect(RuntimeLayout(root).control_db)
    try:  # skip the back-off instead of sleeping through it
        conn.execute("UPDATE run_queue SET not_before=? WHERE run_id='b-new'", (started,))
        conn.commit()
    finally:
        conn.close()
    assert serve.run_once(root, worker="w1") == 1
    row = _queue_row(root, "tenant-b", "b-new")
    assert (row.status, row.attempt) == ("succeeded", 0)  # type: ignore[attr-defined]
    p = open_run_persistence(tdir, runtime_root=root)
    try:
        assert set(p.checkpoints.list_run_ids()) == {"b-new", "b-old"}
        assert _hist(p, "b-old") == [(1, "n1"), (2, "n2")]
    finally:
        p.close()


def _async_request(root: Path, tenant: str, run_id: str) -> object:
    from recertia.workers.run_worker import load_async_request

    tdir = root / "runs" / tenant
    req = load_async_request(
        {
            "run_id": run_id,
            "tenant_id": tenant,
            "request": run_id,
            "budget": {"max_attempts": 1},
            "workdir": str(root / "workspaces" / tenant / run_id),
            "skills_root": str(root / "skills"),
            "facts_root": str(root / "facts"),
            "runs_root": str(tdir),
            "index_path": str(tdir / "skill_index.db"),
        }
    )
    req.workdir.mkdir(parents=True, exist_ok=True)
    return req


def test_embedded_worker_requeues_a_root_busy_claim_while_the_same_tenant_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API's embedded queue worker: RootBusy -> 'queued', and the run's events say
    ``retryable: True`` (they said ``run_failed`` / ``retryable: False`` before). The
    holder is an import of tenant-b itself; until REC-065 it was a tenant-a handle."""

    from recertia.api.events import RunEventLog
    from recertia.workers.embedded import EmbeddedQueueWorker
    from recertia.workers.queue import open_run_queue
    from recertia.workers.run_worker import AsyncRunWorker, dump_async_request

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "rt"
    tdir = _tenant_pending(root, "tenant-b", "b-old")
    req = _async_request(root, "tenant-b", "b-new")
    _enqueue(root, "tenant-b", "b-new", dump_async_request(req))  # type: ignore[arg-type]
    events = RunEventLog(root)
    worker = AsyncRunWorker(events=events)
    queue = open_run_queue(root)
    embedded = EmbeddedQueueWorker(worker, queue, worker_id="embedded-test")
    try:
        before = _run_store_snapshot(tdir)
        release = _hold_root(root, "same_tenant_import")
        try:
            claims = queue.claim("embedded-test", limit=1)
            assert len(claims) == 1
            embedded._execute(claims[0])
            row = queue.get("tenant-b", "b-new")
            assert row is not None
            assert (row.status, row.attempt, row.error_code) == ("queued", 0, "root_busy")
            assert _run_store_snapshot(tdir) == before
        finally:
            release()  # type: ignore[operator]
        errors = [e for e in events.list_after("tenant-b", "b-new") if e.get("type") == "error"]
        payloads = [e["payload"] for e in errors]
        assert any(p.get("code") == "root_busy" and p.get("retryable") is True for p in payloads), errors
        assert not any(p.get("code") == "run_failed" for p in payloads), errors
        finished = [e for e in events.list_after("tenant-b", "b-new") if e.get("type") == "run.finished"]
        assert finished == []
    finally:
        queue.close()
        worker.shutdown(wait=True)


@pytest.mark.parametrize("action", ["start", "resume"])
def test_embedded_worker_requeues_a_root_busy_raised_mid_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """REC-058: ``RootBusy`` raised during ``start``/``resume``, after the orchestrator
    was built, is retryable too. ``AsyncRunWorker._run`` records ``root_busy`` and
    re-raises, and the embedded queue worker re-queues the claim. Without that ``except``
    the error fell into ``run_failed`` (mutant C3), and no other test noticed."""

    from dataclasses import replace

    from recertia.api.events import RunEventLog
    from recertia.graph.engine import GraphOrchestrator
    from recertia.workers.embedded import EmbeddedQueueWorker
    from recertia.workers.queue import open_run_queue
    from recertia.workers.run_worker import AsyncRunWorker, dump_async_request

    calls: list[str] = []

    def root_busy_mid_run(self: object, run_id: str, *args: object, **kwargs: object) -> object:
        calls.append(run_id)
        raise RootBusy("the runtime root lock was taken during the run")

    monkeypatch.setattr(GraphOrchestrator, action, root_busy_mid_run)
    root = tmp_path / "rt"
    req = replace(_async_request(root, "tenant-b", "b-new"), action=action)  # type: ignore[type-var]
    _enqueue(root, "tenant-b", "b-new", dump_async_request(req))  # type: ignore[arg-type]
    events = RunEventLog(root)
    worker = AsyncRunWorker(events=events)
    queue = open_run_queue(root)
    embedded = EmbeddedQueueWorker(worker, queue, worker_id="embedded-test")
    try:
        claims = queue.claim("embedded-test", limit=1)
        assert len(claims) == 1
        embedded._execute(claims[0])
        assert calls == ["b-new"]  # raised mid-run, not while building the orchestrator
        row = queue.get("tenant-b", "b-new")
        assert row is not None
        assert (row.status, row.attempt, row.error_code) == ("queued", 0, "root_busy")
        log = events.list_after("tenant-b", "b-new")
        payloads = [e["payload"] for e in log if e.get("type") == "error"]
        assert any(p.get("code") == "root_busy" and p.get("retryable") is True for p in payloads), log
        assert not any(p.get("code") == "run_failed" for p in payloads), log
        assert [e for e in log if e.get("type") == "run.finished"] == []
        with pytest.raises(RootBusy):  # the worker itself re-raises it to the queue
            worker._run(req)  # type: ignore[arg-type]
        api_lock, lock = _exclusive_root_locks(root, action="backup")  # the bundle was closed
        lock.release()
        api_lock.release()
    finally:
        queue.close()
        worker.shutdown(wait=True)


@pytest.mark.parametrize("path", ["serve", "embedded"])
def test_root_busy_requeue_backs_off_for_root_busy_retry_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """REC-058: a refused claim is due again exactly ``ROOT_BUSY_RETRY_S`` (2 s) after
    the refusal, and a worker that polls inside that window claims nothing. The older
    checks (``due > started``, a second ``run_once`` returns 0) also held with no back-off,
    because the item was re-claimed, refused again and re-queued (mutant C6)."""

    from datetime import timedelta

    import recertia.workers.serve as serve
    from recertia.api.events import RunEventLog
    from recertia.workers.embedded import EmbeddedQueueWorker
    from recertia.workers.queue import ROOT_BUSY_RETRY_S, open_run_queue
    from recertia.workers.run_worker import AsyncRunWorker, dump_async_request

    assert ROOT_BUSY_RETRY_S == 2.0
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "rt"
    _tenant_pending(root, "tenant-b", "b-old")
    if path == "serve":
        payload: dict[str, object] = {"request": "b-new", "budget": {"max_attempts": 1}}
    else:
        payload = dump_async_request(_async_request(root, "tenant-b", "b-new"))  # type: ignore[arg-type]
    _enqueue(root, "tenant-b", "b-new", payload)
    release = _hold_root(root, "backup")
    try:
        lo = datetime.now(timezone.utc)
        if path == "serve":
            assert serve.run_once(root, worker="w1") == 0
        else:
            worker = AsyncRunWorker(events=RunEventLog(root))
            queue = open_run_queue(root)
            try:
                claims = queue.claim("embedded-test", limit=1)
                assert len(claims) == 1
                EmbeddedQueueWorker(worker, queue, worker_id="embedded-test")._execute(claims[0])
            finally:
                queue.close()
                worker.shutdown(wait=True)
        hi = datetime.now(timezone.utc)
        row = _queue_row(root, "tenant-b", "b-new")
        outcome = (row.status, row.attempt, row.error_code)  # type: ignore[attr-defined]
        assert outcome == ("queued", 0, "root_busy")
        due = _queue_due(root, "tenant-b", "b-new")
        back_off = timedelta(seconds=ROOT_BUSY_RETRY_S)
        assert lo + back_off <= datetime.fromisoformat(due) <= hi + back_off
        assert serve.run_once(root, worker="w2") == 0  # polled inside the back-off window
        assert _queue_due(root, "tenant-b", "b-new") == due  # nothing was claimed or re-queued
    finally:
        release()  # type: ignore[operator]


# --- backup/restore leave the root's own lock files alone ---


def test_backup_leaves_out_the_root_lock_files(tmp_path: Path) -> None:
    """Backup holds ``.lock`` and ``api.lock`` exclusively; on Windows those locks are
    mandatory, so reading them through another handle can fail. They carry no data."""

    from recertia.ops.backup import verify_archive

    root = _imported_root(tmp_path)
    layout = RuntimeLayout(root)
    assert layout.root_lock.is_file()
    layout.api_lock.touch()
    archive = tmp_path / "b.tar.gz"
    backup_tree(root, archive)
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
    assert ".lock" not in names and "api.lock" not in names, sorted(names)
    assert "runtime.db" in names
    listed = {entry["path"] for entry in verify_archive(archive)["files"]}
    assert ".lock" not in listed and "api.lock" not in listed


def test_restore_does_not_extract_root_lock_files_from_an_older_archive(tmp_path: Path) -> None:
    """An archive written before this change has ``.lock`` / ``api.lock`` members;
    restore must not write over the files it is holding locked."""

    src = _imported_root(tmp_path)
    archive = tmp_path / "old.tar.gz"
    backup_tree(src, archive)
    legacy = tmp_path / "legacy.tar.gz"
    with tarfile.open(archive) as old, tarfile.open(legacy, "w:gz") as new:
        for member in old.getmembers():
            data = old.extractfile(member) if member.isfile() else None
            new.addfile(member, data)
        for name in (".lock", "./api.lock"):
            info = tarfile.TarInfo(name)
            info.size = 1
            new.addfile(info, io.BytesIO(b"x"))
    target = tmp_path / "target"
    restore_tree(legacy, target)
    layout = RuntimeLayout(target)
    for lock_file in (layout.root_lock, layout.api_lock):
        assert not lock_file.exists() or lock_file.stat().st_size == 0, lock_file
    opened = open_run_persistence(target)
    try:
        assert opened.checkpoints.list_run_ids() == ["old-run"]
    finally:
        opened.close()


# --- a KeyboardInterrupt during wiring still releases the lock ---


def test_keyboard_interrupt_in_orchestrator_wiring_releases_root_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins ``except BaseException`` in build_default_orchestrator (not ``Exception``)."""

    from recertia.bootstrap import build_default_orchestrator
    from recertia.graph.engine import GraphOrchestrator

    root = tmp_path / "runs"

    def interrupted(self: GraphOrchestrator, *args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(GraphOrchestrator, "__init__", interrupted)
    with pytest.raises(KeyboardInterrupt):
        build_default_orchestrator(root, skills_root=tmp_path / "skills", facts_root=tmp_path / "facts")
    assert _backup_locks_free(root)


def test_keyboard_interrupt_in_orchestrator_init_releases_its_own_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins ``except BaseException`` in GraphOrchestrator.__init__ (not ``Exception``)."""

    import recertia.graph.engine as engine

    root = tmp_path / "runs"

    def interrupted() -> list[object]:
        raise KeyboardInterrupt

    monkeypatch.setattr(engine, "default_hop_hooks", interrupted)
    with pytest.raises(KeyboardInterrupt):
        engine.GraphOrchestrator(root)
    assert _backup_locks_free(root)


# --- REC-065 (#158): per-tenant locks under the shared root lock ---


def _assert_opens_imported(root: Path, tdir: Path) -> None:
    """A later open sees the leftover seqs exactly once, importing them if still pending."""

    assert _worker_shaped_open(root, tdir) == [(1, "n1"), (2, "n2")]
    _assert_imported_once(root, tdir)


def _hold_tenant(monkeypatch: pytest.MonkeyPatch, root: Path, tdir: Path, holder: str) -> object:
    """Make ``tdir``'s tenant hold its lock; returns a callable that lets go of it.

    ``import``: a worker-shaped open whose leftover import is paused inside the
    import (root ``.lock`` shared, tenant lock exclusive), via ``_gate_import``.
    ``handle``: a live ``migrate=off`` handle (root ``.lock`` shared, tenant lock
    shared), which keeps the leftover files pending.
    """

    if holder == "handle":
        p = open_run_persistence(tdir, runtime_root=root, migrate="off")
        return p.close
    gate = _gate_import(monkeypatch)
    thread, box = _in_thread(lambda: _worker_shaped_open(root, tdir))
    assert gate["inside"].wait(_JOIN_BOUND_S)  # type: ignore[attr-defined]

    def release() -> None:
        gate["release"].set()  # type: ignore[attr-defined]
        thread.join(_JOIN_BOUND_S)
        assert not thread.is_alive()
        assert box.get("result") == [(1, "n1"), (2, "n2")], box

    return release


@pytest.mark.parametrize("b_state", ["pending", "imported"])
@pytest.mark.parametrize("holder", ["import", "handle"])
def test_two_tenants_on_one_root_hold_locks_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, holder: str, b_state: str
) -> None:
    """Done-when 1: tenant-a holds its tenant lock (an import, or a live handle) and
    tenant-b's open, with its own leftover import when ``pending``, goes ahead. The
    lock timeout is 0, so any wait at all would raise RootBusy."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0")
    root = tmp_path / "rt"
    tdir_a = _tenant_pending(root, "tenant-a", "r")
    tdir_b = _tenant_pending(root, "tenant-b", "r")
    if b_state == "imported":
        assert _worker_shaped_open(root, tdir_b) == [(1, "n1"), (2, "n2")]
    release_a = _hold_tenant(monkeypatch, root, tdir_a, holder)
    try:
        pb = open_run_persistence(tdir_b, runtime_root=root)
        try:
            pb.checkpoints.save("b-new", 1, "b1", None, _state("b-new"))
            assert _hist(pb, "r") == [(1, "n1"), (2, "n2")]
            assert set(pb.checkpoints.list_run_ids()) == {"r", "b-new"}
            assert not _backup_locks_free(root)  # both tenants still keep backup out
        finally:
            pb.close()
        assert legacy_import_complete(RuntimeLayout(root).tenant("tenant-b").runtime_db)
        if holder == "handle":  # tenant-a's leftover files are untouched by tenant-b
            assert legacy_sqlite_pending(RuntimeLayout(root).tenant("tenant-a"))
        assert not _backup_locks_free(root)
    finally:
        release_a()  # type: ignore[operator]
    assert _backup_locks_free(root)
    _assert_opens_imported(root, tdir_a)
    pb = open_run_persistence(tdir_b, runtime_root=root)
    try:
        assert set(pb.checkpoints.list_run_ids()) == {"r", "b-new"}  # nothing of tenant-a
    finally:
        pb.close()


def test_two_tenant_imports_on_one_root_overlap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both tenants' leftover imports are inside ``_import_under_lock`` at once
    (before REC-065 the root lock kept every import on the root one at a time)."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0")
    root = tmp_path / "rt"
    tdir_a = _tenant_pending(root, "tenant-a", "r")
    tdir_b = _tenant_pending(root, "tenant-b", "r")
    gate = _gate_import(monkeypatch)
    thread_a, box_a = _in_thread(lambda: _worker_shaped_open(root, tdir_a))
    try:
        assert gate["inside"].wait(_JOIN_BOUND_S)  # type: ignore[attr-defined]
        assert _worker_shaped_open(root, tdir_b) == [(1, "n1"), (2, "n2")]
    finally:
        gate["release"].set()  # type: ignore[attr-defined]
        thread_a.join(_JOIN_BOUND_S)
    assert not thread_a.is_alive()
    assert box_a.get("result") == [(1, "n1"), (2, "n2")], box_a
    assert gate["entries"] == 2 and gate["max_active"] == 2, gate
    _assert_opens_imported(root, tdir_a)
    _assert_opens_imported(root, tdir_b)


@pytest.mark.parametrize("holder", ["import", "handle"])
def test_same_tenant_still_excluded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, holder: str) -> None:
    """Done-when 2: while tenant-a holds its tenant lock (an import, or a live handle
    that keeps its leftover files pending), a second open of tenant-a that must import
    waits out the bound, then raises typed RootBusy, and writes nothing."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.3")
    root = tmp_path / "rt"
    tdir_a = _tenant_pending(root, "tenant-a", "r")
    layout_a = RuntimeLayout(root).tenant("tenant-a")
    release_a = _hold_tenant(monkeypatch, root, tdir_a, holder)
    try:
        before = _run_store_snapshot(tdir_a)
        started = time.monotonic()
        with pytest.raises(RootBusy, match="tenant lock is held"):
            open_run_persistence(tdir_a, runtime_root=root)
        waited = time.monotonic() - started
        assert 0.25 <= waited <= _JOIN_BOUND_S, waited
        assert _run_store_snapshot(tdir_a) == before
        assert legacy_sqlite_pending(layout_a)
        assert not legacy_import_complete(layout_a.runtime_db)
    finally:
        release_a()  # type: ignore[operator]
    _assert_opens_imported(root, tdir_a)


def test_same_tenant_import_refuses_a_second_import_without_a_root_lock_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An import holds the root ``.lock`` only shared: a refused same-tenant open names
    the tenant lock, and another tenant's handle can still be opened meanwhile."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0")
    root = tmp_path / "rt"
    tdir_a = _tenant_pending(root, "tenant-a", "r")
    release_a = _hold_tenant(monkeypatch, root, tdir_a, "import")
    try:
        with pytest.raises(RootBusy) as busy:
            open_run_persistence(tdir_a, runtime_root=root)
        layout_a = RuntimeLayout(root).tenant("tenant-a")
        assert str(layout_a.tenant_lock) in str(busy.value)
        assert "root lock is held" not in str(busy.value)
        p = open_run_persistence(root / "runs" / "tenant-c", runtime_root=root)
        p.close()
    finally:
        release_a()  # type: ignore[operator]


@pytest.mark.parametrize("action", ["backup", "restore"])
@pytest.mark.parametrize("holder", ["import", "handle"])
def test_backup_and_restore_refused_while_a_tenant_holds_its_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, holder: str, action: str
) -> None:
    """Done-when 3: backup/restore take the root ``.lock`` exclusively, so they still
    refuse while any one tenant holds its tenant lock, and change nothing."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.3")
    root = tmp_path / "rt"
    tdir_a = _tenant_pending(root, "tenant-a", "r")
    tdir_b = _tenant_pending(root, "tenant-b", "r")
    _worker_shaped_open(root, tdir_b)  # tenant-b imported and idle
    archive = tmp_path / "before.tar.gz"
    backup_tree(root, archive)
    out = tmp_path / "during.tar.gz"
    release_a = _hold_tenant(monkeypatch, root, tdir_a, holder)
    try:
        before = {t.name: _run_store_snapshot(t) for t in (tdir_a, tdir_b)}
        started = time.monotonic()
        with pytest.raises(BackupError, match=f"{action} refused: an open run handle, worker or import"):
            if action == "backup":
                backup_tree(root, out)
            else:
                restore_tree(archive, root, overwrite=True)
        # No wait (``try_exclusive``): well under the 0.3 s import lock bound set above.
        assert time.monotonic() - started <= 0.1
        assert not out.exists()
        assert {t.name: _run_store_snapshot(t) for t in (tdir_a, tdir_b)} == before
    finally:
        release_a()  # type: ignore[operator]
    if action == "backup":
        assert backup_tree(root, out) == out.resolve()
    else:
        assert restore_tree(archive, root, overwrite=True) == root.resolve()
    _assert_opens_imported(root, tdir_a)
    _assert_opens_imported(root, tdir_b)


def test_queue_worker_claim_runs_while_another_tenant_handle_is_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``recertia worker`` claims tenant-b's run, importing tenant-b's leftover files,
    while a tenant-a handle is open: no RootBusy, no requeue (REC-058 requeued it)."""

    import recertia.workers.serve as serve

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0")
    root = tmp_path / "rt"
    tdir = _tenant_pending(root, "tenant-b", "b-old")
    _enqueue(root, "tenant-b", "b-new", {"request": "b-new", "budget": {"max_attempts": 1}})
    release = _hold_root(root, "other_tenant_handle")
    try:
        assert serve.run_once(root, worker="w1") == 1
        row = _queue_row(root, "tenant-b", "b-new")
        assert (row.status, row.attempt) == ("succeeded", 0)  # type: ignore[attr-defined]
    finally:
        release()  # type: ignore[operator]
    p = open_run_persistence(tdir, runtime_root=root)
    try:
        assert set(p.checkpoints.list_run_ids()) == {"b-new", "b-old"}
        assert _hist(p, "b-old") == [(1, "n1"), (2, "n2")]
    finally:
        p.close()


def test_tenant_lock_refused_on_the_tenant_lock_holds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``TenantLock``: a refused tenant wait gives the root lock back, waits once within
    the bound (one deadline for both locks), and release frees both for backup."""

    root = tmp_path / "rt"
    layout = RuntimeLayout(root).tenant("tenant-a")
    layout.runs_root.mkdir(parents=True)
    first = TenantLock(RuntimeLayout(root).root_lock, layout.tenant_lock)
    first.acquire(exclusive=True, timeout_s=0)
    # #169, for TenantLock: a refused acquire closes the fds it opened at the OS level
    # (``_fd is None`` also holds when an fd leaks). Checked right after each refusal,
    # before a later open can reuse the fd number on the same file.
    opened = _record_lock_fds(monkeypatch)
    try:
        second = TenantLock(RuntimeLayout(root).root_lock, layout.tenant_lock)
        for exclusive in (True, False):
            started = time.monotonic()
            with pytest.raises(RootLockBusy):
                second.acquire(exclusive=exclusive, timeout_s=0.2)
            assert time.monotonic() - started <= _JOIN_BOUND_S
            assert second.refused == f"tenant lock is held: {layout.tenant_lock}"
            assert second.root._fd is None and second.tenant._fd is None
            assert len(opened) >= 2  # the root lock, then each try on the tenant lock
            assert [fd for fd, st in opened if _still_open(fd, st)] == []
            opened.clear()
        with pytest.raises(ValueError):
            second.acquire(exclusive=True, timeout_s=None)
        assert second.root._fd is None and second.tenant._fd is None
        assert opened == []  # refused before opening anything
    finally:
        first.release()
    assert _backup_locks_free(root)
    other = TenantLock(RuntimeLayout(root).root_lock, RuntimeLayout(root).tenant("tenant-b").tenant_lock)
    api_lock, lock = _exclusive_root_locks(root, action="backup")
    opened.clear()
    try:
        with pytest.raises(RootLockBusy):
            other.acquire(exclusive=False, timeout_s=0)
        assert other.refused == f"root lock is held: {RuntimeLayout(root).root_lock}"
        assert other.root._fd is None and other.tenant._fd is None
        assert len(opened) == 1  # the root lock only; the tenant lock was never opened
        assert [fd for fd, st in opened if _still_open(fd, st)] == []
    finally:
        lock.release()
        api_lock.release()


def test_backup_leaves_out_tenant_lock_files_and_restore_skips_them(tmp_path: Path) -> None:
    """``.tenant.lock`` files are locks only, like ``.lock``: not archived, not restored."""

    root = tmp_path / "rt"
    tdir = _tenant_pending(root, "tenant-a", "r")
    _worker_shaped_open(root, tdir)
    tenant_lock = RuntimeLayout(root).tenant("tenant-a").tenant_lock
    assert tenant_lock.is_file()
    archive = tmp_path / "b.tar.gz"
    backup_tree(root, archive)
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
    assert not any(name.endswith(".tenant.lock") for name in names), sorted(names)
    crafted = tmp_path / "crafted.tar.gz"
    with tarfile.open(archive) as old, tarfile.open(crafted, "w:gz") as new:
        for member in old.getmembers():
            new.addfile(member, old.extractfile(member) if member.isfile() else None)
        info = tarfile.TarInfo("runs/tenant-a/.tenant.lock")
        info.size = 1
        new.addfile(info, io.BytesIO(b"x"))
    target = tmp_path / "target"
    restore_tree(crafted, target)
    restored = RuntimeLayout(target).tenant("tenant-a").tenant_lock
    assert not restored.exists() or restored.stat().st_size == 0
    _assert_imported_once(target, target / "runs" / "tenant-a")


# --- REC-071: follow-ups from the review of #170 ---


def _record_lock_fds(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, os.stat_result]]:
    """Record every fd ``RootLock._open`` hands out, with the file it was opened on."""

    opened: list[tuple[int, os.stat_result]] = []
    original = RootLock._open

    def recording_open(self: RootLock) -> int:
        fd = original(self)
        opened.append((fd, os.fstat(fd)))
        return fd

    monkeypatch.setattr(RootLock, "_open", recording_open)
    return opened


def _still_open(fd: int, opened_on: os.stat_result) -> bool:
    """True if ``fd`` is still open on the same lock file (a reused fd number is not)."""

    try:
        now = os.fstat(fd)
    except OSError:
        return False
    return os.path.samestat(now, opened_on)


@pytest.mark.parametrize("exclusive", [True, False])
def test_root_lock_tenant_lock_one_deadline_covers_both_waits(tmp_path: Path, exclusive: bool) -> None:
    """``TenantLock.acquire(timeout_s=T)`` waits at most ``T`` in total. The root
    lock is held exclusive for most of ``T`` (backup, released by a timer) and the tenant
    lock is held throughout, so the tenant wait gets only what is left of ``T``. With a
    fresh ``T`` for the tenant wait (mutant M7) the call takes about ``T`` + the root wait."""

    root = tmp_path / "rt"
    layout = RuntimeLayout(root).tenant("tenant-a")
    layout.runs_root.mkdir(parents=True)
    root_wait_s, timeout_s = 0.4, 0.6
    tenant_holder = RootLock(layout.tenant_lock)  # the tenant lock alone, held throughout
    tenant_holder.try_exclusive()
    backup = RootLock(RuntimeLayout(root).root_lock)
    backup.try_exclusive()
    releaser = threading.Timer(root_wait_s, backup.release)
    releaser.start()
    waiter = TenantLock(RuntimeLayout(root).root_lock, layout.tenant_lock)
    try:
        started = time.monotonic()
        with pytest.raises(RootLockBusy):
            waiter.acquire(exclusive=exclusive, timeout_s=timeout_s)
        waited = time.monotonic() - started
    finally:
        releaser.join(_JOIN_BOUND_S)
        tenant_holder.release()
    assert waiter.refused == f"tenant lock is held: {layout.tenant_lock}"  # it got past the root
    # One deadline: about timeout_s. M7 would be about root_wait_s + timeout_s (1.0 s).
    assert timeout_s - 0.05 <= waited < timeout_s + 0.25, waited
    assert waiter.root._fd is None and waiter.tenant._fd is None
    assert _backup_locks_free(root)


@pytest.mark.parametrize(
    "rel",
    [
        "snapshots/s1/.tenant.lock",
        "workspaces/acme/w1/.tenant.lock",
        "runs/acme/notes/.tenant.lock",
        "blobs/acme/.tenant.lock",
    ],
)
def test_root_lock_backup_keeps_a_tenant_lock_name_outside_a_runs_root(tmp_path: Path, rel: str) -> None:
    """Only a runs root's ``.tenant.lock`` is a lock file. A file of that name
    anywhere else is user content: backup archives it and restore brings it back."""

    root = tmp_path / "rt"
    tdir = _tenant_pending(root, "tenant-a", "r")
    _worker_shaped_open(root, tdir)
    user_file = root / rel
    user_file.parent.mkdir(parents=True, exist_ok=True)
    user_file.write_bytes(b"user content")
    archive = tmp_path / "b.tar.gz"
    backup_tree(root, archive)
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
    assert rel in names, sorted(names)
    assert "runs/tenant-a/.tenant.lock" not in names  # the real tenant lock stays out
    target = tmp_path / "target"
    restore_tree(archive, target)
    assert (target / rel).read_bytes() == b"user content"


# --- REC-072: follow-ups from the review of #177 ---


_TENANT_LOCK_POSITIONS = {
    "cli-layout-root": lambda layout: layout.local_runs().tenant_lock,
    "cli-layout-jobs": lambda layout: layout.local_runs().jobs().tenant_lock,
    "tenant-runs-root": lambda layout: layout.tenant("tenant-a").tenant_lock,
    "tenant-jobs": lambda layout: layout.tenant("tenant-a").jobs().tenant_lock,
}


@pytest.mark.parametrize("position", sorted(_TENANT_LOCK_POSITIONS))
def test_root_lock_backup_and_restore_skip_every_runs_root_tenant_lock(tmp_path: Path, position: str) -> None:
    """Each of the four runs-root positions of ``.tenant.lock`` is a lock file: backup does
    not archive it, and restore extracts neither a ``/`` nor a ``\\`` crafted member there.

    The runtime root itself (CLI layout) and ``runs/<t>`` are runs roots, each with a
    ``jobs`` runs root under it."""

    root = tmp_path / "rt"
    tdir = _tenant_pending(root, "tenant-a", "r")
    _worker_shaped_open(root, tdir)
    lock_path = _TENANT_LOCK_POSITIONS[position](RuntimeLayout(root))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch()
    rel = lock_path.relative_to(root).as_posix()
    assert rel in {
        ".tenant.lock",
        "jobs/.tenant.lock",
        "runs/tenant-a/.tenant.lock",
        "runs/tenant-a/jobs/.tenant.lock",
    }

    archive = tmp_path / "b.tar.gz"
    backup_tree(root, archive)
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
    assert rel not in names, sorted(names)
    assert "runs/tenant-a/runtime.db" in names  # the backup itself happened

    crafted_body = b"crafted lock content"
    for index, name in enumerate((rel, rel.replace("/", "\\"))):
        crafted = tmp_path / "crafted.tar.gz"
        with tarfile.open(archive) as old, tarfile.open(crafted, "w:gz") as new:
            for member in old.getmembers():
                new.addfile(member, old.extractfile(member) if member.isfile() else None)
            info = tarfile.TarInfo(name)
            info.size = len(crafted_body)
            new.addfile(info, io.BytesIO(crafted_body))
        target = tmp_path / f"target-{index}"
        restore_tree(crafted, target)
        written = [p for p in target.rglob("*") if p.is_file() and p.read_bytes() == crafted_body]
        assert written == [], (name, written)
