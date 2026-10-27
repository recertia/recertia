"""REC-058: the root lock against the real ``kernel32`` on a Windows host.

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
# Upper bound for a wait that RECERTIA_IMPORT_LOCK_TIMEOUT_S limits to 0.3 s. It is well
# under the production default (5 s) and the 10 s retry budget, so a read of the wrong
# variable or an unbounded wait fails by seconds, not by a few milliseconds.
_OFF_BOUND_S = 2.0
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
    """REC-058 (refs #153, REC-073): ``migrate=off`` with pending leftover files takes
    the shared root lock with a bounded wait (REC-064): at most
    ``RECERTIA_IMPORT_LOCK_TIMEOUT_S``, in non-blocking ``LockFileEx`` tries. Against a
    real cross-process exclusive holder the open raises typed ``RootBusy`` after that
    bound, not after the ``LockFileEx`` retry budget and not as a bare ``OSError``; the
    cause is the non-blocking refusal (``EAGAIN``). The unbounded wait (``timeout_s=None``)
    still ends in ``RootLockBusy`` over ``EDEADLK`` once the retry budget runs out. The
    retry after release writes to the leftover files."""

    root = tmp_path / "runs"
    p = open_run_persistence(root, migrate="off")
    try:
        p.checkpoints.save("old", 1, "n1", None, _state("old"))
    finally:
        p.close()
    # The bound is the env timeout, not the 10 s retry budget (left at its default here).
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.3")
    holder = _Holder(RuntimeLayout(root).root_lock, "exclusive")
    try:
        started = time.monotonic()
        with pytest.raises(RootBusy) as info:
            open_run_persistence(root, migrate="off")
        assert 0.3 <= time.monotonic() - started <= _OFF_BOUND_S
        busy = info.value.__cause__
        assert isinstance(busy, RootLockBusy)
        assert isinstance(busy.__cause__, BlockingIOError)
        assert busy.__cause__.errno == errno.EAGAIN
        # The unbounded wait keeps the LockFileEx retry budget and its EDEADLK refusal.
        monkeypatch.setattr(root_lock, "_WIN_BLOCKING_RETRY_S", 0.3)
        unbounded = RootLock(RuntimeLayout(root).root_lock)
        started = time.monotonic()
        with pytest.raises(RootLockBusy) as budget:
            unbounded.acquire_shared()
        assert 0.3 <= time.monotonic() - started <= _OFF_BOUND_S
        assert isinstance(budget.value.__cause__, OSError)
        assert budget.value.__cause__.errno == errno.EDEADLK
        assert unbounded._fd is None
    finally:
        holder.stop()
    p = open_run_persistence(root, migrate="off")
    try:
        p.checkpoints.save("old", 2, "n2", None, _state("old"))
        assert _hist(p, "old") == [(1, "n1"), (2, "n2")]
    finally:
        p.close()


# --- REC-065 (#158): per-tenant locks against a real cross-process holder ---

_TENANT_HOLDER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from recertia.persistence.root_lock import TenantLock

    lock = TenantLock(Path(sys.argv[1]), Path(sys.argv[2]))
    lock.acquire(exclusive=sys.argv[3] == "exclusive", timeout_s=1.0)
    print("held", flush=True)
    sys.stdin.read()
    lock.release()
    """
)


class _TenantHolder(_Holder):
    """A child process holding ``<root>/.lock`` shared and one tenant lock until stopped:
    ``exclusive`` is what a leftover import holds, ``shared`` what a run handle holds."""

    def __init__(self, root: Path, tenant: str, mode: str) -> None:
        layout = RuntimeLayout(root)
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _TENANT_HOLDER,
                str(layout.root_lock),
                str(layout.tenant(tenant).tenant_lock),
                mode,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        assert line.strip() == "held", line


@pytest.mark.parametrize("mode", ["exclusive", "shared"])
def test_cross_process_tenant_holder_lets_another_tenant_open_and_keeps_backup_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Tenant-a's import (``exclusive``) or handle (``shared``) in another process; a
    tenant-b open needs no wait (timeout 0), and backup is refused until it stops."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0")
    root = tmp_path / "rt"
    tdir_a, tdir_b = root / "runs" / "tenant-a", root / "runs" / "tenant-b"
    for tdir in (tdir_a, tdir_b):
        open_run_persistence(tdir, runtime_root=root).close()
    holder = _TenantHolder(root, "tenant-a", mode)
    try:
        p = open_run_persistence(tdir_b, runtime_root=root)
        try:
            p.checkpoints.save("b", 1, "n1", None, _state("b"))
        finally:
            p.close()
        with pytest.raises(BackupError, match="backup refused"):
            backup_tree(root, tmp_path / "refused.tar.gz")
    finally:
        holder.stop()
    assert backup_tree(root, tmp_path / "ok.tar.gz") == (tmp_path / "ok.tar.gz").resolve()


def test_cross_process_tenant_import_makes_a_same_tenant_open_root_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tenant-a's import in another process holds its tenant lock exclusively; a tenant-a
    open waits within the bound, then raises typed ``RootBusy`` naming the tenant lock."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.3")
    root = tmp_path / "rt"
    tdir_a = root / "runs" / "tenant-a"
    open_run_persistence(tdir_a, runtime_root=root).close()
    holder = _TenantHolder(root, "tenant-a", "exclusive")
    try:
        started = time.monotonic()
        with pytest.raises(RootBusy, match="tenant lock is held"):
            open_run_persistence(tdir_a, runtime_root=root)
        assert 0.3 <= time.monotonic() - started <= _BOUND_S
    finally:
        holder.stop()
    p = open_run_persistence(tdir_a, runtime_root=root)
    try:
        p.checkpoints.save("a", 1, "n1", None, _state("a"))
        assert _hist(p, "a") == [(1, "n1")]
    finally:
        p.close()


# --- review of #169: the refused handle is closed, non-busy errors ---


def _record_lock_fds(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, os.stat_result]]:
    opened: list[tuple[int, os.stat_result]] = []
    original = RootLock._open

    def recording_open(self: RootLock) -> int:
        fd = original(self)
        opened.append((fd, os.fstat(fd)))
        return fd

    monkeypatch.setattr(RootLock, "_open", recording_open)
    return opened


def _record_open_times(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Monotonic time at which each ``RootLock._open`` call began (wraps the current one)."""
    starts: list[float] = []
    original = RootLock._open

    def timed_open(self: RootLock) -> int:
        starts.append(time.monotonic())
        return original(self)

    monkeypatch.setattr(RootLock, "_open", timed_open)
    return starts


def _still_open(fd: int, opened_on: os.stat_result) -> bool:
    try:
        now = os.fstat(fd)
    except OSError:
        return False
    return os.path.samestat(now, opened_on)


def test_root_lock_migrate_off_root_busy_closes_the_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REC-073: the bounded ``migrate=off`` wait that runs out against a
    cross-process holder raises ``RootBusy`` and closes every fd (and so every handle) it
    opened on ``.lock``. The bounded wait retries, opening one fd per try, so there are
    several and none may stay open."""

    root = tmp_path / "runs"
    p = open_run_persistence(root, migrate="off")
    try:
        p.checkpoints.save("old", 1, "n1", None, _state("old"))
    finally:
        p.close()
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.3")
    holder = _Holder(RuntimeLayout(root).root_lock, "exclusive")
    opened = _record_lock_fds(monkeypatch)
    starts = _record_open_times(monkeypatch)
    try:
        started = time.monotonic()
        with pytest.raises(RootBusy):
            open_run_persistence(root, migrate="off")
        elapsed = time.monotonic() - started
    finally:
        holder.stop()
    lock_stat = os.stat(RuntimeLayout(root).root_lock)
    assert elapsed <= _OFF_BOUND_S
    # It retried: one open (and one close) per try. A single try is accepted only when it
    # began at once and itself used the whole 0.3 s bound (a stall inside that one open,
    # e.g. a scanner on a hosted runner); a single try that began late (sleep, then one
    # try) or ended early (bound of zero) still fails.
    assert len(opened) >= 1
    assert len(opened) >= 2 or (starts[0] - started < 0.1 and elapsed >= 0.3), (len(opened), elapsed)
    assert all(os.path.samestat(st, lock_stat) for _fd, st in opened)
    assert [fd for fd, st in opened if _still_open(fd, st)] == []


@pytest.mark.parametrize("call", ["blocking_shared", "try_exclusive"])
def test_root_lock_real_error_other_than_33_is_oserror_not_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    """A real ``LockFileEx`` failure that is not ``ERROR_LOCK_VIOLATION`` (here a
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
