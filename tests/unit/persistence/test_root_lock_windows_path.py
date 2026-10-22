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
import os
import threading
import time
from pathlib import Path

import pytest

import recertia.persistence.root_lock as root_lock
from recertia.graph.persistence import open_run_persistence
from recertia.ops.backup import BackupError, _exclusive_root_locks
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
    with pytest.raises(OSError) as info:
        waiter.acquire_shared()
    assert info.value.errno == errno.EDEADLK
    assert time.monotonic() - started >= 0.2
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
