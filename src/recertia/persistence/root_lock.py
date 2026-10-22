"""Shared/exclusive lock for one runtime root, and per-tenant locks under it.

The API holds a shared lock for its lifetime. Backup and restore take an
exclusive lock and refuse when the shared lock is already held.

Lock order (REC-065). Every runtime root has one root lock, ``<root>/.lock``,
and every runs root under it (a tenant's ``<root>/runs/<tenant>``, or the CLI
layout's root itself) has its own tenant lock, ``<runs_root>/.tenant.lock``.
Locks are always taken in this order and released in reverse:

1. ``api.lock`` (backup and restore only; the API holds it shared),
2. the root ``.lock``,
3. one tenant lock.

- The leftover import takes the root lock SHARED, then its tenant lock
  EXCLUSIVE, for the length of the import.
- A run handle (``open_run_persistence``) takes the root lock SHARED, then its
  tenant lock SHARED, for the life of the handle.
- Backup and restore take the root lock EXCLUSIVE and no tenant lock. As every
  tenant holder also holds the root lock shared, that excludes all tenants.

So tenants on one root do not exclude each other, an import still excludes
every other import and handle of its own tenant, and backup/restore still
exclude every tenant. Nothing holds a tenant lock without the root lock, and
nothing waits for the root lock while holding a tenant lock, so the order has
no cycle. (The one exception is ``graph/ops.py``'s blocking exclusive
``<db_parent>/.lock`` for the operations schema, #160: in the CLI layout that is
the root lock. It is unreachable from a run handle today, because
``open_run_persistence`` runs ``ensure_operations_schema`` first.) A tenant lock
waits within the same bound as the root lock (:class:`TenantLock`).

The lock file is a flock target only. It must not record pid/tid identity:
a SIGKILLed holder leaves an empty file, and leftover text is never treated
as holding the lock.

On Windows the same shared/exclusive modes use ``LockFileEx`` on byte 0
(``msvcrt.locking`` has no shared mode: its ``LK_LOCK`` is an exclusive byte
lock). Locking past end-of-file is allowed, so the lock file stays empty.
Blocking calls keep the old ``msvcrt`` budget: they retry for
``_WIN_BLOCKING_RETRY_S`` and then raise (``RootLockBusy`` from a blocking
``acquire_shared``).
"""

from __future__ import annotations

import errno
import os
import sys
import time
from pathlib import Path
from typing import Protocol

_IS_WINDOWS = sys.platform == "win32"

# winbase.h / winerror.h
_LOCKFILE_FAIL_IMMEDIATELY = 0x1
_LOCKFILE_EXCLUSIVE_LOCK = 0x2
_ERROR_LOCK_VIOLATION = 33
_WIN_BLOCKING_RETRY_S = 10.0  # msvcrt LK_LOCK: 10 attempts, 1 s apart


class _WinLockApi(Protocol):
    """The ``kernel32`` calls the Windows lock path needs (one byte at offset 0)."""

    def handle(self, fd: int) -> int: ...

    def lock(self, handle: int, flags: int) -> bool: ...

    def unlock(self, handle: int) -> bool: ...

    def last_error(self) -> int: ...


if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    class _Kernel32LockApi:
        def __init__(self) -> None:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._lock = k32.LockFileEx
            self._lock.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(_Overlapped),
            ]
            self._lock.restype = wintypes.BOOL
            self._unlock = k32.UnlockFileEx
            self._unlock.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(_Overlapped),
            ]
            self._unlock.restype = wintypes.BOOL

        def handle(self, fd: int) -> int:
            return int(msvcrt.get_osfhandle(fd))

        def lock(self, handle: int, flags: int) -> bool:
            return bool(self._lock(handle, flags, 0, 1, 0, ctypes.byref(_Overlapped())))

        def unlock(self, handle: int) -> bool:
            return bool(self._unlock(handle, 0, 1, 0, ctypes.byref(_Overlapped())))

        def last_error(self) -> int:
            return int(ctypes.get_last_error())

    def _win_error(code: int, what: str) -> OSError:
        del what
        return ctypes.WinError(code)  # keeps .winerror

    _LOCK_SH = _LOCK_EX = _LOCK_NB = _LOCK_UN = 0

    def _flock(fd: int, op: int) -> None:
        raise OSError(errno.ENOSYS, "flock is not available on Windows")
else:
    import fcntl

    _LOCK_SH, _LOCK_EX, _LOCK_NB, _LOCK_UN = fcntl.LOCK_SH, fcntl.LOCK_EX, fcntl.LOCK_NB, fcntl.LOCK_UN

    def _flock(fd: int, op: int) -> None:
        fcntl.flock(fd, op)

    def _win_error(code: int, what: str) -> OSError:
        # Only reached through the fake kernel32 in tests; real Windows uses ctypes.WinError.
        exc = OSError(errno.EIO, f"{what} failed (winerror {code})")
        exc.winerror = code  # type: ignore[attr-defined]
        return exc


_WIN_API: _WinLockApi | None = None


def _win_api() -> _WinLockApi:
    global _WIN_API
    if _WIN_API is None:
        _WIN_API = _load_win_api()
    return _WIN_API


def _load_win_api() -> _WinLockApi:
    if sys.platform == "win32":
        return _Kernel32LockApi()
    raise OSError(errno.ENOSYS, "LockFileEx is only available on Windows")


class RootLockBusy(RuntimeError):
    """An exclusive lock could not be taken because the root is in use."""


class RootBusy(RootLockBusy):
    """Typed busy error: an open timed out waiting for the root lock.

    Raised by the leftover import and by ``open_run_persistence`` on an
    already-imported root while another holder has an exclusive lock they need:
    backup or restore (the root lock), or an import of the same tenant (its
    tenant lock). An import also gets it while a handle of its own tenant is open.

    Callers must not write somewhere else. A generic ``Exception`` or a hang is
    not this error.
    """


class RootLock:
    """One open file description for ``root/.lock`` (or any lock path)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire_shared(self, *, timeout_s: float | None = None) -> None:
        """Shared lock. Compatible with other shared holders.

        Blocks until granted when ``timeout_s`` is ``None`` (on Windows, for at
        most the ``LockFileEx`` retry budget, then ``RootLockBusy``). Otherwise
        waits at most ``timeout_s`` for an exclusive holder and raises
        ``RootLockBusy``.
        """

        if timeout_s is None:
            fd = self._open()
            try:
                _lock_shared(fd)
            except OSError as exc:
                os.close(fd)
                if _is_busy(exc):
                    # Windows: the retry budget ran out (OSError(EDEADLK)). Typed busy, as
                    # for the bounded wait, so callers can raise RootBusy (REC-058 S-1).
                    raise RootLockBusy(f"root lock held: {self.path}") from exc
                raise
            self._fd = fd
            return
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            try:
                self._try_shared()
                return
            except RootLockBusy:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    def _try_shared(self) -> None:
        fd = self._open()
        try:
            _lock_shared_nb(fd)
        except BlockingIOError as exc:
            os.close(fd)
            raise RootLockBusy(f"root lock held: {self.path}") from exc
        except OSError as exc:
            os.close(fd)
            if _is_busy(exc):
                raise RootLockBusy(f"root lock held: {self.path}") from exc
            raise
        self._fd = fd

    def try_exclusive(self) -> None:
        """Non-blocking exclusive lock. Raises ``RootLockBusy`` when held."""

        fd = self._open()
        try:
            _lock_exclusive_nb(fd)
        except BlockingIOError as exc:
            os.close(fd)
            raise RootLockBusy(f"root lock held: {self.path}") from exc
        except OSError as exc:
            os.close(fd)
            if _is_busy(exc):
                raise RootLockBusy(f"root lock held: {self.path}") from exc
            raise
        self._fd = fd

    def acquire_exclusive(self, *, timeout_s: float | None = None) -> None:
        """Blocking exclusive lock. Raises ``RootLockBusy`` when ``timeout_s`` elapses."""

        if timeout_s is None:
            fd = self._open()
            try:
                _lock_exclusive(fd)
            except OSError:
                os.close(fd)
                raise
            self._fd = fd
            return
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            try:
                self.try_exclusive()
                return
            except RootLockBusy:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    def release(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            _unlock(fd)
        finally:
            os.close(fd)

    def _open(self) -> int:
        if self._fd is not None:
            raise RuntimeError(f"root lock already held: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)


class TenantLock:
    """The root lock (always shared), then one tenant lock, in that order (REC-065).

    ``acquire(exclusive=True)`` is the leftover import; ``exclusive=False`` is a
    run handle. Backup and restore take the root lock exclusively instead, which
    any holder of this lock keeps out. See the module docstring for the order.
    """

    def __init__(self, root_lock: Path, tenant_lock: Path) -> None:
        self.root = RootLock(root_lock)
        self.tenant = RootLock(tenant_lock)
        # After a refused ``acquire``: which lock was held, for the caller's RootBusy.
        self.refused = ""

    @property
    def path(self) -> Path:
        return self.tenant.path

    def acquire(self, *, exclusive: bool, timeout_s: float | None) -> None:
        """Root lock shared, then the tenant lock. Raises ``RootLockBusy`` from either.

        With a ``timeout_s`` both waits share one deadline, so the whole call
        waits at most ``timeout_s``. ``None`` blocks on each lock like
        ``RootLock.acquire_shared`` (shared only: an exclusive tenant lock is
        always bounded). On a refusal nothing is held.
        """

        if exclusive and timeout_s is None:
            raise ValueError("an exclusive tenant lock needs a timeout")
        deadline = None if timeout_s is None else time.monotonic() + max(0.0, timeout_s)
        self.refused = ""
        try:
            self.root.acquire_shared(timeout_s=timeout_s)
        except RootLockBusy:
            self.refused = f"root lock is held: {self.root.path}"
            raise
        try:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if exclusive:
                self.tenant.acquire_exclusive(timeout_s=remaining)
            else:
                self.tenant.acquire_shared(timeout_s=remaining)
        except BaseException as exc:
            if isinstance(exc, RootLockBusy):
                self.refused = f"tenant lock is held: {self.tenant.path}"
            self.root.release()
            raise

    def release(self) -> None:
        """Tenant lock first, then the root lock (the reverse of ``acquire``).

        The order is not observable from outside: once the root lock is free,
        backup or restore may take it while the tenant fd is still closing, and
        neither takes a tenant lock. It is kept so release mirrors ``acquire``
        and nothing ever holds a tenant lock without the root lock.
        """

        try:
            self.tenant.release()
        finally:
            self.root.release()


def _lock_shared(fd: int) -> None:
    if _IS_WINDOWS:
        _win_lock(fd, exclusive=False, blocking=True)
        return
    _flock(fd, _LOCK_SH)


def _lock_shared_nb(fd: int) -> None:
    if _IS_WINDOWS:
        _win_lock(fd, exclusive=False, blocking=False)
        return
    _flock(fd, _LOCK_SH | _LOCK_NB)


def _lock_exclusive_nb(fd: int) -> None:
    if _IS_WINDOWS:
        _win_lock(fd, exclusive=True, blocking=False)
        return
    _flock(fd, _LOCK_EX | _LOCK_NB)


def _lock_exclusive(fd: int) -> None:
    if _IS_WINDOWS:
        _win_lock(fd, exclusive=True, blocking=True)
        return
    _flock(fd, _LOCK_EX)


def _unlock(fd: int) -> None:
    if _IS_WINDOWS:
        api = _win_api()
        if not api.unlock(api.handle(fd)):
            # The caller still closes the handle, which drops the lock; report why.
            raise _win_error(api.last_error(), "UnlockFileEx")
        return
    _flock(fd, _LOCK_UN)


def _win_lock(fd: int, *, exclusive: bool, blocking: bool) -> None:
    """``LockFileEx`` on byte 0, always fail-immediately; blocking retries within a budget."""

    api = _win_api()
    handle = api.handle(fd)
    flags = _LOCKFILE_FAIL_IMMEDIATELY | (_LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0)
    deadline = time.monotonic() + (_WIN_BLOCKING_RETRY_S if blocking else 0.0)
    while True:
        if api.lock(handle, flags):
            return
        err = api.last_error()
        if err != _ERROR_LOCK_VIOLATION:
            raise _win_error(err, "LockFileEx")  # not busy: keeps its winerror
        if not blocking:
            raise BlockingIOError(errno.EAGAIN, "root lock held (LockFileEx)")
        if time.monotonic() >= deadline:
            raise OSError(errno.EDEADLK, "root lock still held after the LockFileEx retry budget")
        time.sleep(0.05)


_WIN_BUSY_ERRNOS = frozenset({errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)})
_POSIX_BUSY_ERRNOS = frozenset({errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES})


def _is_busy(exc: OSError) -> bool:
    """True when an error raised by a lock call means "held by someone else".

    Only lock-call errors reach this; ``_open`` is outside the ``try``, so an
    ``EACCES`` from ``os.open`` is never read as busy.

    - Windows: ``winerror`` 33 (``ERROR_LOCK_VIOLATION``, from ``LockFileEx``).
      An error with any other ``winerror`` is not busy and propagates with it
      (``ctypes.WinError`` maps e.g. ``ERROR_ACCESS_DENIED`` to ``EACCES``, which
      must not read as busy). An errno-only error (``msvcrt.locking`` sets no
      ``winerror``) is busy for ``EACCES`` (``LK_NBLCK`` contention) or
      ``EDEADLOCK`` (``LK_LOCK`` after its 10 retries).
    - POSIX: ``EAGAIN`` / ``EWOULDBLOCK`` / ``EACCES`` (``fcntl``/``lockf`` may
      report a conflict as ``EACCES``).
    """

    if _IS_WINDOWS:
        winerror = getattr(exc, "winerror", None)
        if winerror is not None:
            return bool(winerror == _ERROR_LOCK_VIOLATION)
        return exc.errno in _WIN_BUSY_ERRNOS
    return exc.errno in _POSIX_BUSY_ERRNOS
