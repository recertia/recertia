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
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

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
    # REC-058: the budget's OSError(EDEADLK) is typed busy (it was raised as is).
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


# --- busy mapping, no _win_prepare, UnlockFileEx result ---


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


# --- refs #153: the blocking migrate=off wait ends in typed RootBusy ---


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
    """``migrate=off`` with pending leftover files now uses a bounded shared wait
    (REC-064). On Windows that ends in typed ``RootBusy`` (which both worker paths
    re-queue). Nothing is written and no lock is kept."""

    root = tmp_path / "runs"
    p = open_run_persistence(root, migrate="off")
    try:
        p.checkpoints.save("old", 1, "n1", None, _state("old"))
    finally:
        p.close()
    assert legacy_sqlite_pending(RuntimeLayout(root).local_runs())
    before = _file_bytes(root)
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.2")
    holder = RootLock(RuntimeLayout(root).root_lock)
    holder.try_exclusive()  # backup, restore or an import
    try:
        started = time.monotonic()
        with pytest.raises(RootBusy) as info:
            open_run_persistence(root, migrate="off")
        assert time.monotonic() - started >= 0.15  # it did wait out the bound
        busy = info.value.__cause__
        assert isinstance(busy, RootLockBusy)
        assert isinstance(busy.__cause__, OSError)
        # Bounded wait uses non-blocking tries, so EAGAIN, not the old blocking EDEADLK.
        assert busy.__cause__.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK)
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


# --- REC-065 (#158): per-tenant locks through the Windows branch ---


def test_windows_tenants_on_one_root_lock_apart_and_backup_is_refused(
    tmp_path: Path, kernel32: FakeKernel32, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tenant-a's import holds its tenant lock exclusively (LockFileEx with the
    exclusive flag) under a shared root lock; tenant-b opens at once, a second tenant-a
    import gets ``RootBusy`` and backup is refused. Nothing stays held."""

    from recertia.persistence.root_lock import TenantLock

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0")
    root = tmp_path / "rt"
    layout = RuntimeLayout(root)
    for tenant in ("tenant-a", "tenant-b"):
        layout.tenant(tenant).runs_root.mkdir(parents=True)
    import_a = TenantLock(layout.root_lock, layout.tenant("tenant-a").tenant_lock)
    import_a.acquire(exclusive=True, timeout_s=0)
    try:
        assert sorted(kernel32.held.values()) == ["exclusive", "shared"]
        pb = open_run_persistence(layout.tenant("tenant-b").runs_root, runtime_root=root)
        try:
            pb.checkpoints.save("b", 1, "n1", None, _state("b"))
        finally:
            pb.close()
        second_a = TenantLock(layout.root_lock, layout.tenant("tenant-a").tenant_lock)
        with pytest.raises(RootLockBusy):
            second_a.acquire(exclusive=True, timeout_s=0.1)
        assert second_a.refused.startswith("tenant lock is held")
        with pytest.raises(RootBusy):
            open_run_persistence(layout.tenant("tenant-a").runs_root, runtime_root=root)
        with pytest.raises(BackupError, match="backup refused"):
            _exclusive_root_locks(root, action="backup")
        assert sorted(kernel32.held.values()) == ["exclusive", "shared"]
    finally:
        import_a.release()
    assert kernel32.held == {}
    api_lock, lock = _exclusive_root_locks(root, action="backup")
    lock.release()
    api_lock.release()


# --- review of #169: the refused fd is closed, non-busy errors ---


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


class _SteppedClock:
    """Stand-in for ``root_lock.time``: the clock moves only when the code sleeps.

    The bounded lock wait polls with ``time.sleep``, so against the real clock the number
    of polls depends on scheduler and timer granularity. With this clock it is exact. The
    tick is a power of two, so the arithmetic below is exact in binary floating point.
    """

    tick_s = 0.0625

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += self.tick_s


def test_root_lock_windows_blocking_typed_busy_closes_the_lock_fd(
    tmp_path: Path, kernel32: FakeKernel32, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the blocking ``acquire_shared`` runs out of its ``LockFileEx`` budget and
    raises typed ``RootLockBusy``, the fd it opened is closed, not leaked."""

    monkeypatch.setattr(root_lock, "_WIN_BLOCKING_RETRY_S", 0.1)
    holder = RootLock(tmp_path / ".lock")
    holder.try_exclusive()
    opened = _record_lock_fds(monkeypatch)
    waiter = RootLock(tmp_path / ".lock")
    try:
        with pytest.raises(RootLockBusy):
            waiter.acquire_shared()
    finally:
        holder.release()
    assert len(opened) == 1
    fd, opened_on = opened[0]
    assert not _still_open(fd, opened_on)
    assert waiter._fd is None


def test_root_lock_windows_migrate_off_root_busy_closes_the_lock_fd(
    tmp_path: Path, kernel32: FakeKernel32, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through ``open_run_persistence``: the ``migrate=off`` wait that ends in typed
    ``RootBusy`` closes the root-lock fd it opened, so a worker that re-queues the claim
    does not leak one handle per refused attempt."""

    root = tmp_path / "runs"
    p = open_run_persistence(root, migrate="off")
    try:
        p.checkpoints.save("old", 1, "n1", None, _state("old"))
    finally:
        p.close()
    monkeypatch.setattr(root_lock, "_WIN_BLOCKING_RETRY_S", 0.1)
    lock_path = RuntimeLayout(root).root_lock
    holder = RootLock(lock_path)
    holder.try_exclusive()
    opened = _record_lock_fds(monkeypatch)
    # A 0.25 s wait on a 1/16 s tick polls at 0, 1/16, 2/16, 3/16 and 4/16 s: 5 opens
    # (each one refused and closed) per refused attempt, so 15 for 3 attempts.
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    clock = _SteppedClock()
    monkeypatch.setattr(root_lock, "time", clock)
    attempts = 3
    opens_per_attempt = 5
    try:
        for _attempt in range(attempts):
            with pytest.raises(RootBusy):
                open_run_persistence(root, migrate="off")
    finally:
        holder.release()
    assert len(opened) == attempts * opens_per_attempt
    # The stepped clock ignores how long each sleep asks for, so pin the poll interval here:
    # 4 sleeps (one between each pair of polls) per attempt, every one of 10 ms.
    assert clock.sleeps == [0.01] * (attempts * (opens_per_attempt - 1))
    assert all(os.path.samestat(st, os.stat(lock_path)) for _fd, st in opened)
    assert [fd for fd, st in opened if _still_open(fd, st)] == []
    assert kernel32.held == {}


def _fail_lock_with(kernel32: FakeKernel32, monkeypatch: pytest.MonkeyPatch, winerror: int) -> None:
    def failing_lock(handle: int, flags: int) -> bool:
        kernel32.calls.append(("lock", handle, flags))
        kernel32._last_error = winerror
        return False

    monkeypatch.setattr(kernel32, "lock", failing_lock)


# ERROR_INVALID_HANDLE, ERROR_INVALID_PARAMETER, ERROR_DISK_FULL: none is 33, and none
# maps to EACCES / EDEADLOCK. On Windows ctypes.WinError maps them to EBADF, EINVAL and
# ENOSPC; off Windows the fake's OSError carries errno EIO. (ERROR_SHARING_BUFFER_EXCEEDED,
# 36, is not usable here: on Windows it maps to EACCES.)
_NOT_BUSY_WINERRORS = (6, 87, 112)


@pytest.mark.parametrize("winerror", _NOT_BUSY_WINERRORS)
@pytest.mark.parametrize("call", ["blocking_shared", "bounded_shared", "try_exclusive"])
def test_root_lock_windows_error_other_than_33_is_oserror_not_busy(
    tmp_path: Path,
    kernel32: FakeKernel32,
    monkeypatch: pytest.MonkeyPatch,
    call: str,
    winerror: int,
) -> None:
    """A ``LockFileEx`` error other than ``ERROR_LOCK_VIOLATION`` (33) whose errno is
    not ``EACCES`` / ``EDEADLOCK`` propagates as ``OSError`` with its ``winerror`` on every
    entry point, including the blocking typed path; it is never ``RootLockBusy``. The fd
    is closed either way."""

    _fail_lock_with(kernel32, monkeypatch, winerror)
    opened = _record_lock_fds(monkeypatch)
    lock = RootLock(tmp_path / ".lock")
    with pytest.raises(OSError) as info:
        if call == "blocking_shared":
            lock.acquire_shared()
        elif call == "bounded_shared":
            lock.acquire_shared(timeout_s=0.05)
        else:
            lock.try_exclusive()
    assert not isinstance(info.value, RootLockBusy)
    assert getattr(info.value, "winerror", None) == winerror
    assert info.value.errno not in root_lock._WIN_BUSY_ERRNOS
    assert len([op for op, _h, _f in kernel32.calls if op == "lock"]) == 1  # no busy retry
    assert [fd for fd, st in opened if _still_open(fd, st)] == []
    assert lock._fd is None


@pytest.mark.parametrize("call", ["blocking_shared", "bounded_shared", "try_exclusive"])
def test_root_lock_windows_access_denied_with_busy_errno_is_oserror_not_busy(
    tmp_path: Path,
    kernel32: FakeKernel32,
    monkeypatch: pytest.MonkeyPatch,
    call: str,
) -> None:
    """The case that tells winerror and errno apart: ``ERROR_ACCESS_DENIED`` (5)
    carries errno ``EACCES``, which is busy for an errno-only error. The ``winerror``
    decides, so every entry point, the blocking typed path included, raises that
    ``OSError`` once, never ``RootLockBusy``, and closes the fd. On Windows
    ``ctypes.WinError(5)`` has errno ``EACCES``; off Windows the fake's error is given
    the same errno so the case discriminates on Linux too."""

    if sys.platform != "win32":

        def win_error_with_eacces(code: int, what: str) -> OSError:
            exc = OSError(errno.EACCES, f"{what} failed (winerror {code})")
            exc.winerror = code  # type: ignore[attr-defined]
            return exc

        monkeypatch.setattr(root_lock, "_win_error", win_error_with_eacces)
    _fail_lock_with(kernel32, monkeypatch, 5)  # ERROR_ACCESS_DENIED
    opened = _record_lock_fds(monkeypatch)
    lock = RootLock(tmp_path / ".lock")
    with pytest.raises(OSError) as info:
        if call == "blocking_shared":
            lock.acquire_shared()
        elif call == "bounded_shared":
            lock.acquire_shared(timeout_s=0.05)
        else:
            lock.try_exclusive()
    assert info.value.errno == errno.EACCES  # a busy errno: only the winerror says no
    assert getattr(info.value, "winerror", None) == 5
    assert not isinstance(info.value, RootLockBusy)
    assert len([op for op, _h, _f in kernel32.calls if op == "lock"]) == 1  # no busy retry
    assert [fd for fd, st in opened if _still_open(fd, st)] == []
    assert lock._fd is None


@pytest.mark.parametrize("migrate", ["off", "auto"])
def test_root_lock_windows_open_error_other_than_33_is_oserror_not_root_busy(
    tmp_path: Path,
    kernel32: FakeKernel32,
    monkeypatch: pytest.MonkeyPatch,
    migrate: Literal["off", "auto"],
) -> None:
    """Through ``open_run_persistence``: with ``migrate=off`` (pending leftover files,
    the blocking wait) and with the bounded runtime-db wait, a non-busy ``LockFileEx``
    error is raised as that ``OSError``, not as ``RootBusy``, which a worker would
    re-queue forever instead of failing the claim with the real cause."""

    root = tmp_path / "runs"
    p = open_run_persistence(root, migrate=migrate)
    try:
        p.checkpoints.save("old", 1, "n1", None, _state("old"))
    finally:
        p.close()
    before = _file_bytes(root)
    _fail_lock_with(kernel32, monkeypatch, 87)  # ERROR_INVALID_PARAMETER
    with pytest.raises(OSError) as info:
        open_run_persistence(root, migrate=migrate)
    assert not isinstance(info.value, RootLockBusy)
    assert getattr(info.value, "winerror", None) == 87
    assert _file_bytes(root) == before
    assert kernel32.held == {}
