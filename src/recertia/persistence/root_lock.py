"""Shared/exclusive lock for one runtime root.

The API holds a shared lock for its lifetime. Backup and restore take an
exclusive lock and refuse when the shared lock is already held.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class RootLockBusy(RuntimeError):
    """An exclusive lock could not be taken because the root is in use."""


class RootBusy(RootLockBusy):
    """Typed busy error: leftover import timed out waiting for the root lock.

    Callers must not write somewhere else. A generic ``Exception`` or a hang is
    not this error.
    """


class RootLock:
    """One open file description for ``root/.lock`` (or any lock path)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire_shared(self) -> None:
        """Blocking shared lock. Compatible with other shared holders."""

        fd = self._open()
        try:
            _lock_shared(fd)
        except OSError:
            os.close(fd)
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
        _record_exclusive_holder(fd)
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
            _record_exclusive_holder(fd)
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
            _clear_exclusive_holder(fd)
            _unlock(fd)
        finally:
            os.close(fd)

    def _open(self) -> int:
        if self._fd is not None:
            raise RuntimeError(f"root lock already held: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)


def exclusive_held_by_this_thread(path: Path) -> bool:
    """True when this thread recorded the exclusive holder identity in ``path``."""

    holder = exclusive_holder(path)
    return holder == (os.getpid(), threading.get_ident())


def exclusive_holder(path: Path) -> tuple[int, int] | None:
    try:
        text = Path(path).read_text(encoding="ascii").strip()
    except OSError:
        return None
    parts = text.split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _record_exclusive_holder(fd: int) -> None:
    if sys.platform == "win32":
        return
    payload = f"{os.getpid()} {threading.get_ident()}\n".encode("ascii")
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, payload)
    os.ftruncate(fd, len(payload))


def _clear_exclusive_holder(fd: int) -> None:
    if sys.platform == "win32":
        return
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
    except OSError:
        pass


def _lock_shared(fd: int) -> None:
    if sys.platform == "win32":
        _win_prepare(fd)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        return
    fcntl.flock(fd, fcntl.LOCK_SH)


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
