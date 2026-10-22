"""REC-058 (#144): writers on a runtime root honour backup/restore's exclusive root lock.

Kept out of ``test_import_legacy.py`` so that file stays byte-identical to the
ADR-0022 baseline (N3 guard).
"""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.criteria import TaskCriterion
from contracts.run import RunManifest, RunState, Task
from recertia.graph.persistence import open_run_persistence
from recertia.ops.backup import BackupError, _exclusive_root_locks, backup_tree
from recertia.persistence.import_legacy import legacy_import_complete, legacy_sqlite_pending
from recertia.persistence.layout import RuntimeLayout
from recertia.persistence.root_lock import RootBusy


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


_BUSY_BOUND_S = 5.0  # RECERTIA_IMPORT_LOCK_TIMEOUT_S (0.25s here) plus slack


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
        worker.join(timeout=_BUSY_BOUND_S)
        blocked = worker.is_alive()
        if not blocked:
            assert isinstance(box.get("error"), RootBusy), box
            assert float(box["elapsed"]) <= _BUSY_BOUND_S  # type: ignore[arg-type]
        assert box["wrote"] is False
        assert _db_byte_snapshot(root) == before
    finally:
        lock.release()
        api_lock.release()
    worker.join(timeout=_BUSY_BOUND_S)
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
        worker.join(timeout=_BUSY_BOUND_S)
        assert box["wrote"] is False
        assert _db_byte_snapshot(root) == before
    finally:
        lock.release()
        api_lock.release()
    worker.join(timeout=_BUSY_BOUND_S)
    assert not worker.is_alive()
    if not box["wrote"]:
        # Refused while busy: the same write, retried after release, lands.
        assert isinstance(box.get("error"), RootBusy), box
        retry, box = _start_writer(root, "busy-run")
        retry.join(timeout=_BUSY_BOUND_S)
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
        worker.join(timeout=_BUSY_BOUND_S)
        assert not worker.is_alive(), "open hung while the root lock was held"
        assert isinstance(box.get("error"), RootBusy), box
        assert "cannot open the runtime root while the root lock is held" in str(box["error"])
        assert float(box["elapsed"]) <= _BUSY_BOUND_S  # type: ignore[arg-type]
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
