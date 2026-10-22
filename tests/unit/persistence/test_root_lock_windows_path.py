"""REC-058 S2: the Windows lock path has a real shared mode (``LockFileEx``).

CI runs on Linux, so these tests drive ``root_lock``'s Windows branch with
``_IS_WINDOWS`` forced on and a fake ``kernel32`` that models Windows
byte-range lock rules for byte 0: shared locks coexist, an exclusive lock
excludes everything, and a fail-immediately request that conflicts returns
FALSE with ``ERROR_LOCK_VIOLATION`` (33).

What this proves: the Windows branch asks for the right mode and flags, maps
a conflict to ``RootLockBusy`` / ``RootBusy``, keeps the old msvcrt-style
bounded retry for blocking calls, and lets two run handles share a root
while backup is refused. What it does not prove: the real ``ctypes``
signatures (``_Kernel32LockApi``), ``msvcrt.get_osfhandle``, or kernel
behaviour on an actual Windows host. Those are type-checked with
``mypy --platform win32`` only.
"""

from __future__ import annotations

import errno
import hashlib
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import recertia.persistence.root_lock as root_lock
from contracts.criteria import TaskCriterion
from contracts.run import RunManifest, RunState, Task
from recertia.graph.persistence import open_run_persistence
from recertia.ops.backup import BackupError, _exclusive_root_locks
from recertia.persistence.import_legacy import legacy_sqlite_pending
from recertia.persistence.layout import RuntimeLayout
from recertia.persistence.root_lock import RootBusy, RootLock, RootLockBusy


class FakeKernel32:
    """Windows byte-range lock rules for byte 0 of each file, per file handle.

    The fake's handle is the fd itself; the file is identified by inode.
    """

    def __init__(self) -> None:
        self.held: dict[tuple[int, int], str] = {}
        self.calls: list[tuple[str, int, int]] = []
        self._last_error = 0

    def handle(self, fd: int) -> int:
        return fd

    def lock(self, handle: int, flags: int) -> bool:
        self.calls.append(("lock", handle, flags))
        file_id = os.fstat(handle).st_ino
        exclusive = bool(flags & root_lock._LOCKFILE_EXCLUSIVE_LOCK)
        others = [mode for (f, h), mode in self.held.items() if f == file_id and h != handle]
        conflict = bool(others) if exclusive else "exclusive" in others
        if conflict:
            assert flags & root_lock._LOCKFILE_FAIL_IMMEDIATELY, "a blocking LockFileEx would hang"
            self._last_error = root_lock._ERROR_LOCK_VIOLATION
            return False
        self.held[(file_id, handle)] = "exclusive" if exclusive else "shared"
        return True

    def unlock(self, handle: int) -> bool:
        self.calls.append(("unlock", handle, 0))
        if self.held.pop((os.fstat(handle).st_ino, handle), None) is None:
            self._last_error = 158  # ERROR_NOT_LOCKED
            return False
        return True

    def last_error(self) -> int:
        return self._last_error


@pytest.fixture()
def kernel32(monkeypatch: pytest.MonkeyPatch) -> FakeKernel32:
    fake = FakeKernel32()
    monkeypatch.setattr(root_lock, "_IS_WINDOWS", True)
    monkeypatch.setattr(root_lock, "_WIN_API", fake)
    return fake


def test_windows_shared_locks_coexist_and_exclude_an_exclusive(
    tmp_path: Path, kernel32: FakeKernel32
) -> None:
    path = tmp_path / ".lock"
    a, b, ex = RootLock(path), RootLock(path), RootLock(path)
    a.acquire_shared(timeout_s=0.1)
    b.acquire_shared(timeout_s=0.1)  # msvcrt.locking had no shared mode: this was refused
    with pytest.raises(RootLockBusy):
        ex.try_exclusive()
    a.release()
    b.release()
    ex.try_exclusive()
    other = RootLock(path)
    with pytest.raises(RootLockBusy):
        other.acquire_shared(timeout_s=0.05)
    ex.release()
    assert kernel32.held == {}


def test_windows_flags_match_the_requested_mode(tmp_path: Path, kernel32: FakeKernel32) -> None:
    fail_now = root_lock._LOCKFILE_FAIL_IMMEDIATELY
    exclusive = root_lock._LOCKFILE_EXCLUSIVE_LOCK
    shared = RootLock(tmp_path / ".lock")
    shared.acquire_shared()
    shared.release()
    excl = RootLock(tmp_path / ".lock")
    excl.try_exclusive()
    excl.release()
    locks = [flags for op, _h, flags in kernel32.calls if op == "lock"]
    assert locks == [fail_now, fail_now | exclusive]
    assert [op for op, _h, _f in kernel32.calls] == ["lock", "unlock", "lock", "unlock"]


def test_windows_blocking_call_keeps_a_bounded_retry(
    tmp_path: Path, kernel32: FakeKernel32, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocking calls wait within the budget (like msvcrt LK_LOCK), then raise."""

    monkeypatch.setattr(root_lock, "_WIN_BLOCKING_RETRY_S", 2.0)
    holder = RootLock(tmp_path / ".lock")
    holder.try_exclusive()
    releaser = threading.Timer(0.2, holder.release)
    releaser.start()
    waiter = RootLock(tmp_path / ".lock")
    try:
        waiter.acquire_shared()  # held when called; granted once the holder lets go
    finally:
        releaser.join()
    waiter.release()

    monkeypatch.setattr(root_lock, "_WIN_BLOCKING_RETRY_S", 0.2)
    holder.try_exclusive()
    started = time.monotonic()
    # REC-058 S-1: the budget's OSError(EDEADLK) is typed busy (it was raised as is).
    with pytest.raises(RootLockBusy) as info:
        waiter.acquire_shared()
    assert isinstance(info.value.__cause__, OSError)
    assert info.value.__cause__.errno == errno.EDEADLK
    assert time.monotonic() - started >= 0.2
    assert waiter._fd is None  # the refused handle is closed, not kept
    holder.release()


def test_windows_two_run_handles_share_a_root_and_backup_is_refused(
    tmp_path: Path, kernel32: FakeKernel32, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The S2 regression: a worker run plus an API read on one root (Windows API host)."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "runs"
    first = open_run_persistence(root)
    try:
        second = open_run_persistence(root)
        try:
            with pytest.raises(BackupError, match="backup refused"):
                _exclusive_root_locks(root, action="backup")
        finally:
            second.close()
    finally:
        first.close()
    api_lock, lock = _exclusive_root_locks(root, action="backup")
    try:
        with pytest.raises(RootBusy):
            open_run_persistence(root)
    finally:
        lock.release()
        api_lock.release()
    assert kernel32.held == {}


# --- round 3: busy mapping, no _win_prepare, UnlockFileEx result ---


def _oserror(code: int, winerror: int | None = None) -> OSError:
    exc = OSError(code, os.strerror(code))
    if winerror is not None:
        exc.winerror = winerror  # type: ignore[attr-defined]
    return exc


_EDEADLOCK = getattr(errno, "EDEADLOCK", errno.EDEADLK)


@pytest.mark.parametrize(
    ("exc", "busy"),
    [
        (_oserror(errno.EACCES, 33), True),  # LockFileEx ERROR_LOCK_VIOLATION as WinError(33)
        (_oserror(errno.EIO, 33), True),
        (_oserror(errno.EACCES), True),  # msvcrt LK_NBLCK contention: errno only
        (_oserror(_EDEADLOCK), True),  # msvcrt LK_LOCK after 10 retries: errno only
        (_oserror(errno.EACCES, 5), False),  # ERROR_ACCESS_DENIED keeps its winerror
        (_oserror(errno.EINVAL, 87), False),  # ERROR_INVALID_PARAMETER
        (_oserror(errno.EIO, 36), False),  # ERROR_SHARING_BUFFER_EXCEEDED is not a conflict
        (_oserror(errno.EAGAIN), False),
        (_oserror(errno.ENOENT), False),
    ],
)
def test_windows_busy_mapping(monkeypatch: pytest.MonkeyPatch, exc: OSError, busy: bool) -> None:
    monkeypatch.setattr(root_lock, "_IS_WINDOWS", True)
    assert root_lock._is_busy(exc) is busy


@pytest.mark.parametrize(
    ("code", "busy"),
    [
        (errno.EAGAIN, True),
        (errno.EWOULDBLOCK, True),
        (errno.EACCES, True),  # fcntl/lockf may report a conflict as EACCES
        (errno.EDEADLK, False),
        (errno.EIO, False),
        (errno.EBADF, False),
    ],
)
def test_posix_busy_mapping(monkeypatch: pytest.MonkeyPatch, code: int, busy: bool) -> None:
    monkeypatch.setattr(root_lock, "_IS_WINDOWS", False)
    assert root_lock._is_busy(_oserror(code)) is busy


def test_permission_denied_on_open_is_not_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the lock call maps to busy; an EACCES from ``os.open`` propagates as is."""

    def denied(*args: object, **kwargs: object) -> int:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(root_lock.os, "open", denied)
    for windows in (True, False):
        monkeypatch.setattr(root_lock, "_IS_WINDOWS", windows)
        lock = RootLock(tmp_path / ".lock")
        with pytest.raises(PermissionError) as info:
            lock.try_exclusive()
        assert not isinstance(info.value, RootLockBusy)
        with pytest.raises(PermissionError):
            lock.acquire_shared(timeout_s=0.05)


def test_windows_lock_file_stays_empty(tmp_path: Path, kernel32: FakeKernel32) -> None:
    """No ``_win_prepare`` byte: LockFileEx may lock past end-of-file, and an empty file
    has no locked byte for backup's read to trip over."""

    path = tmp_path / ".lock"
    a, b = RootLock(path), RootLock(path)
    a.acquire_shared(timeout_s=0.1)
    b.acquire_shared(timeout_s=0.1)
    assert path.stat().st_size == 0
    a.release()
    b.release()
    ex = RootLock(path)
    ex.try_exclusive()
    assert path.stat().st_size == 0
    ex.release()


def test_windows_lockfileex_error_other_than_33_keeps_its_winerror(
    tmp_path: Path, kernel32: FakeKernel32, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_lock(handle: int, flags: int) -> bool:
        kernel32._last_error = 87  # ERROR_INVALID_PARAMETER
        return False

    monkeypatch.setattr(kernel32, "lock", failing_lock)
    lock = RootLock(tmp_path / ".lock")
    with pytest.raises(OSError) as info:
        lock.try_exclusive()
    assert not isinstance(info.value, RootLockBusy)
    assert getattr(info.value, "winerror", None) == 87


def test_windows_unlock_failure_is_reported_and_the_handle_still_closes(
    tmp_path: Path, kernel32: FakeKernel32, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = RootLock(tmp_path / ".lock")
    lock.try_exclusive()
    fd = lock._fd
    assert fd is not None

    def failing_unlock(handle: int) -> bool:
        kernel32._last_error = 158  # ERROR_NOT_LOCKED
        return False

    monkeypatch.setattr(kernel32, "unlock", failing_unlock)
    with pytest.raises(OSError) as info:
        lock.release()
    assert getattr(info.value, "winerror", None) == 158
    assert lock._fd is None
    with pytest.raises(OSError):
        os.fstat(fd)  # closed, so the OS drops the lock with the handle


# --- round 4 (S-1, refs #153): the blocking migrate=off wait ends in typed RootBusy ---


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


def _file_bytes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_windows_migrate_off_blocking_wait_raises_root_busy(
    tmp_path: Path, kernel32: FakeKernel32, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``migrate=off`` with pending leftover files takes the shared root lock with no
    timeout. On Windows that wait ends after the ``LockFileEx`` retry budget, and it must
    end in typed ``RootBusy`` (which both worker paths re-queue), not ``OSError(EDEADLK)``
    (which a worker fails for good). Nothing is written and no lock is kept."""

    root = tmp_path / "runs"
    p = open_run_persistence(root, migrate="off")
    try:
        p.checkpoints.save("old", 1, "n1", None, _state("old"))
    finally:
        p.close()
    assert legacy_sqlite_pending(RuntimeLayout(root).local_runs())
    before = _file_bytes(root)
    monkeypatch.setattr(root_lock, "_WIN_BLOCKING_RETRY_S", 0.2)
    holder = RootLock(RuntimeLayout(root).root_lock)
    holder.try_exclusive()  # backup, restore or an import
    try:
        started = time.monotonic()
        with pytest.raises(RootBusy) as info:
            open_run_persistence(root, migrate="off")
        assert time.monotonic() - started >= 0.2  # it did wait out the budget
        busy = info.value.__cause__
        assert isinstance(busy, RootLockBusy)
        assert isinstance(busy.__cause__, OSError)
        assert busy.__cause__.errno == errno.EDEADLK
        assert list(kernel32.held.values()) == ["exclusive"]  # only the holder's lock
        assert _file_bytes(root) == before
    finally:
        holder.release()
    p = open_run_persistence(root, migrate="off")  # the retry goes through
    try:
        p.checkpoints.save("old", 2, "n2", None, _state("old"))
        assert [seq for seq, *_rest in p.checkpoints.history("old")] == [1, 2]
    finally:
        p.close()
    assert kernel32.held == {}
