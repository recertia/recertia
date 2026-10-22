"""Shared/exclusive lock for one runtime root.

The API holds a shared lock for its lifetime. Backup and restore take an
exclusive lock and refuse when the shared lock is already held.

The lock file is a flock target only. It must not record pid/tid identity:
a SIGKILLed holder leaves an empty file, and leftover text is never treated
as holding the lock.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class RootLockBusy(RuntimeError):
    """An exclusive lock could not be taken because the root is in use."""


class RootBusy(RootLockBusy):
    """Typed busy error: an open timed out waiting for the root lock.

    Raised by the leftover import and by ``open_run_persistence`` on an
    already-imported root while another holder (backup, restore, an import)
    has the exclusive lock.

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

        Blocks until granted when ``timeout_s`` is ``None``. Otherwise waits at
        most ``timeout_s`` for an exclusive holder and raises ``RootLockBusy``.
        """

        if timeout_s is None:
            fd = self._open()
            try:
                _lock_shared(fd)
            except OSError:
                os.close(fd)
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


def _lock_shared(fd: int) -> None:
    if sys.platform == "win32":
        _win_prepare(fd)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        return
    fcntl.flock(fd, fcntl.LOCK_SH)


def _lock_shared_nb(fd: int) -> None:
    if sys.platform == "win32":
        _win_prepare(fd)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return
    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)


def _lock_exclusive_nb(fd: int) -> None:
    if sys.platform == "win32":
        _win_prepare(fd)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _lock_exclusive(fd: int) -> None:
    if sys.platform == "win32":
        _win_prepare(fd)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        return
    fcntl.flock(fd, fcntl.LOCK_EX)


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    fcntl.flock(fd, fcntl.LOCK_UN)


def _win_prepare(fd: int) -> None:
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)


def _is_busy(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) in {33, 36} or exc.errno in {11, 35}
