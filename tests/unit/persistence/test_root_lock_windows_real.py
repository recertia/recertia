"""REC-058 MF-D: the root lock against the real ``kernel32`` on a Windows host.

``test_root_lock_windows_path.py`` drives the Windows branch on Linux through a
fake ``kernel32``. These tests use the real ``LockFileEx`` / ``UnlockFileEx``
binding (``_Kernel32LockApi``) and run only on Windows, in ci.yml's
``root lock (windows-latest)`` job (``-k root_lock`` and ``-m windows``).
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import recertia.persistence.root_lock as root_lock
from contracts.criteria import TaskCriterion
from contracts.run import RunManifest, RunState, Task
from recertia.graph.persistence import open_run_persistence
from recertia.ops.backup import BackupError, backup_tree, restore_tree, verify_archive
from recertia.persistence.layout import RuntimeLayout
from recertia.persistence.root_lock import RootBusy, RootLock, RootLockBusy

pytestmark = [pytest.mark.windows, pytest.mark.skipif(sys.platform != "win32", reason="real Windows only")]

_BOUND_S = 5.0
_ERROR_NOT_LOCKED = 158


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


def _hist(p: object, run_id: str) -> list[tuple[int, str]]:
    return [(seq, node) for seq, node, *_rest in p.checkpoints.history(run_id)]  # type: ignore[attr-defined]


def test_the_real_binding_is_in_use() -> None:
    assert root_lock._IS_WINDOWS is True
    assert type(root_lock._win_api()).__name__ == "_Kernel32LockApi"


def test_overlapped_struct_size() -> None:
    import ctypes

    expected = 32 if ctypes.sizeof(ctypes.c_void_p) == 8 else 20
    assert ctypes.sizeof(root_lock._Overlapped) == expected  # type: ignore[attr-defined]


def test_shared_locks_coexist(tmp_path: Path) -> None:
    path = tmp_path / ".lock"
    a, b = RootLock(path), RootLock(path)
    a.acquire_shared(timeout_s=0.1)
    try:
        b.acquire_shared(timeout_s=0.1)  # msvcrt.locking had no shared mode: this was refused
        b.release()
    finally:
        a.release()


def test_exclusive_is_busy_while_shared_is_held(tmp_path: Path) -> None:
    path = tmp_path / ".lock"
    a, b, ex = RootLock(path), RootLock(path), RootLock(path)
    a.acquire_shared(timeout_s=0.1)
    b.acquire_shared(timeout_s=0.1)
    try:
        with pytest.raises(RootLockBusy):
            ex.try_exclusive()
        a.release()
        with pytest.raises(RootLockBusy):
            ex.try_exclusive()  # still refused while either shared holder remains
    finally:
        a.release()
        b.release()
    ex.try_exclusive()
    ex.release()


def test_shared_is_busy_while_exclusive_is_held(tmp_path: Path) -> None:
    path = tmp_path / ".lock"
    ex, sh = RootLock(path), RootLock(path)
    ex.try_exclusive()
    try:
        started = time.monotonic()
        with pytest.raises(RootLockBusy):
            sh.acquire_shared(timeout_s=0.2)
        assert time.monotonic() - started >= 0.2  # the bounded wait really waited
    finally:
        ex.release()
    sh.acquire_shared(timeout_s=0.1)
    sh.release()


def test_exclusive_is_busy_while_exclusive_is_held(tmp_path: Path) -> None:
    path = tmp_path / ".lock"
    first, second = RootLock(path), RootLock(path)
    first.try_exclusive()
    try:
        with pytest.raises(RootLockBusy):
            second.try_exclusive()
        with pytest.raises(RootLockBusy):
            second.acquire_exclusive(timeout_s=0.1)
    finally:
        first.release()
    second.try_exclusive()
    second.release()


def test_lock_file_stays_empty(tmp_path: Path) -> None:
    path = tmp_path / ".lock"
    lock = RootLock(path)
    lock.try_exclusive()
    try:
        assert path.stat().st_size == 0
    finally:
        lock.release()


def test_unlock_really_unlocks(tmp_path: Path) -> None:
    """``UnlockFileEx`` returns TRUE, frees byte 0, and reports ERROR_NOT_LOCKED twice."""

    api = root_lock._win_api()
    exclusive = root_lock._LOCKFILE_FAIL_IMMEDIATELY | root_lock._LOCKFILE_EXCLUSIVE_LOCK
    path = tmp_path / ".lock"
    fd1 = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fd2 = os.open(path, os.O_RDWR)
    try:
        h1, h2 = api.handle(fd1), api.handle(fd2)
        assert api.lock(h1, exclusive) is True
        assert api.lock(h2, exclusive) is False
        assert api.last_error() == root_lock._ERROR_LOCK_VIOLATION
        assert api.unlock(h1) is True
        assert api.lock(h2, exclusive) is True  # re-lock through the other handle
        assert api.unlock(h2) is True
        assert api.unlock(h2) is False
        assert api.last_error() == _ERROR_NOT_LOCKED
    finally:
        os.close(fd2)
        os.close(fd1)


def test_release_frees_the_lock_for_the_next_holder(tmp_path: Path) -> None:
    path = tmp_path / ".lock"
    for _ in range(3):
        lock = RootLock(path)
        lock.try_exclusive()
        lock.release()
        assert lock._fd is None


_HOLDER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from recertia.persistence.root_lock import RootLock

    lock = RootLock(Path(sys.argv[1]))
    if sys.argv[2] == "exclusive":
        lock.try_exclusive()
    else:
        lock.acquire_shared(timeout_s=1.0)
    print("held", flush=True)
    sys.stdin.read()
    lock.release()
    """
)


class _Holder:
    """A child process that holds ``path`` (exclusive or shared) until stopped."""

    def __init__(self, path: Path, mode: str) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(path), mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        assert line.strip() == "held", line

    def stop(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        assert self.proc.wait(timeout=_BOUND_S) == 0


def test_cross_process_exclusive_makes_open_root_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.5")
    root = tmp_path / "runs"
    open_run_persistence(root).close()
    holder = _Holder(RuntimeLayout(root).root_lock, "exclusive")
    try:
        started = time.monotonic()
        with pytest.raises(RootBusy):
            open_run_persistence(root)
        assert time.monotonic() - started <= _BOUND_S
    finally:
        holder.stop()
    p = open_run_persistence(root)
    try:
        p.checkpoints.save("after", 1, "n", None, _state("after"))
    finally:
        p.close()


def test_cross_process_shared_coexists_and_keeps_backup_out(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    open_run_persistence(root).close()
    holder = _Holder(RuntimeLayout(root).root_lock, "shared")
    try:
        p = open_run_persistence(root)  # shared here + shared in the child
        p.close()
        with pytest.raises(BackupError, match="backup refused"):
            backup_tree(root, tmp_path / "refused.tar.gz")
    finally:
        holder.stop()
    backup_tree(root, tmp_path / "ok.tar.gz")


def test_two_handles_keep_backup_out_until_both_close(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    archive = tmp_path / "b.tar.gz"
    first = open_run_persistence(root)
    try:
        second = open_run_persistence(root)
        try:
            with pytest.raises(BackupError, match="backup refused"):
                backup_tree(root, archive)
        finally:
            second.close()
        with pytest.raises(BackupError, match="backup refused"):
            backup_tree(root, archive)
    finally:
        first.close()
    assert backup_tree(root, archive) == archive.resolve()


def test_backup_then_restore_round_trip(tmp_path: Path) -> None:
    """Settles the mandatory-lock question: backup and restore hold ``.lock`` and
    ``api.lock`` exclusively, and neither reads nor writes those files."""

    root = tmp_path / "runs"
    p = open_run_persistence(root)
    try:
        for seq in (1, 2):
            p.checkpoints.save("r", seq, f"n{seq}", None, _state("r"))
    finally:
        p.close()
    layout = RuntimeLayout(root)
    layout.api_lock.touch()
    assert layout.root_lock.is_file() and layout.api_lock.is_file()
    archive = tmp_path / "b.tar.gz"
    backup_tree(root, archive)
    verify_archive(archive)
    for target, overwrite in ((tmp_path / "fresh", False), (root, True)):
        restore_tree(archive, target, overwrite=overwrite)
        q = open_run_persistence(target)
        try:
            assert _hist(q, "r") == [(1, "n1"), (2, "n2")]
        finally:
            q.close()


def test_migrate_off_blocking_wait_raises_root_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """REC-058 S-1 (refs #153): ``migrate=off`` with pending leftover files waits for the
    shared root lock without a timeout; against a real cross-process exclusive holder the
    ``LockFileEx`` retry budget runs out and the open raises typed ``RootBusy``, not
    ``OSError(EDEADLK)``. The retry after release writes to the leftover files."""

    root = tmp_path / "runs"
    p = open_run_persistence(root, migrate="off")
    try:
        p.checkpoints.save("old", 1, "n1", None, _state("old"))
    finally:
        p.close()
    monkeypatch.setattr(root_lock, "_WIN_BLOCKING_RETRY_S", 0.3)
    holder = _Holder(RuntimeLayout(root).root_lock, "exclusive")
    try:
        started = time.monotonic()
        with pytest.raises(RootBusy) as info:
            open_run_persistence(root, migrate="off")
        assert 0.3 <= time.monotonic() - started <= _BOUND_S
        busy = info.value.__cause__
        assert isinstance(busy, RootLockBusy)
        assert isinstance(busy.__cause__, OSError)
        assert busy.__cause__.errno == errno.EDEADLK
    finally:
        holder.stop()
    p = open_run_persistence(root, migrate="off")
    try:
        p.checkpoints.save("old", 2, "n2", None, _state("old"))
        assert _hist(p, "old") == [(1, "n1"), (2, "n2")]
    finally:
        p.close()


# --- round 5 (CTO-rec review of #169): N-1 the refused handle is closed, N-3 non-busy errors ---


def _record_lock_fds(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, os.stat_result]]:
    opened: list[tuple[int, os.stat_result]] = []
    original = RootLock._open

    def recording_open(self: RootLock) -> int:
        fd = original(self)
        opened.append((fd, os.fstat(fd)))
        return fd

    monkeypatch.setattr(RootLock, "_open", recording_open)
    return opened


def _still_open(fd: int, opened_on: os.stat_result) -> bool:
    try:
        now = os.fstat(fd)
    except OSError:
        return False
    return os.path.samestat(now, opened_on)


def test_root_lock_migrate_off_root_busy_closes_the_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N-1: the blocking ``migrate=off`` wait that runs out of the real ``LockFileEx``
    budget against a cross-process holder raises ``RootBusy`` and closes the fd (and so
    the handle) it opened on ``.lock``."""

    root = tmp_path / "runs"
    p = open_run_persistence(root, migrate="off")
    try:
        p.checkpoints.save("old", 1, "n1", None, _state("old"))
    finally:
        p.close()
    monkeypatch.setattr(root_lock, "_WIN_BLOCKING_RETRY_S", 0.3)
    holder = _Holder(RuntimeLayout(root).root_lock, "exclusive")
    opened = _record_lock_fds(monkeypatch)
    try:
        with pytest.raises(RootBusy):
            open_run_persistence(root, migrate="off")
    finally:
        holder.stop()
    assert len(opened) == 1
    fd, opened_on = opened[0]
    assert not _still_open(fd, opened_on)


@pytest.mark.parametrize("call", ["blocking_shared", "try_exclusive"])
def test_root_lock_real_error_other_than_33_is_oserror_not_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    """N-3: a real ``LockFileEx`` failure that is not ``ERROR_LOCK_VIOLATION`` (here a
    NULL handle, ``ERROR_INVALID_HANDLE``) propagates as ``OSError`` with its ``winerror``,
    on the blocking typed path too; it is never ``RootLockBusy``. The fd is closed."""

    api = root_lock._win_api()
    monkeypatch.setattr(api, "handle", lambda fd: 0)
    opened = _record_lock_fds(monkeypatch)
    lock = RootLock(tmp_path / ".lock")
    started = time.monotonic()
    with pytest.raises(OSError) as info:
        if call == "blocking_shared":
            lock.acquire_shared()
        else:
            lock.try_exclusive()
    assert time.monotonic() - started < root_lock._WIN_BLOCKING_RETRY_S  # no busy retry
    assert not isinstance(info.value, RootLockBusy)
    winerror = getattr(info.value, "winerror", None)
    assert winerror is not None and winerror != root_lock._ERROR_LOCK_VIOLATION
    assert info.value.errno not in root_lock._WIN_BUSY_ERRNOS
    assert [fd for fd, st in opened if _still_open(fd, st)] == []
    assert lock._fd is None
