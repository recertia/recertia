"""One flock owner per run. The fencing token advances on each acquire."""

from __future__ import annotations

import errno
import os
import sys
import threading
from pathlib import Path

_THREAD_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()


class RunBusy(RuntimeError):
    """Another process holds this run's lease."""


def _thread_lock(key: str) -> threading.Lock:
    with _GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


class RunLease:
    """Exclusive lease held for the duration of start or resume."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.token: int | None = None
        self.incarnation: int = 1
        self._fd: int | None = None
        self._held = False
        self._thread = _thread_lock(str(path))

    def acquire(self) -> int:
        if not self._thread.acquire(blocking=False):
            raise RunBusy(f"run lease busy: {self.path.name}")
        self._held = True
        fd: int | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            _flock_exclusive(fd)
            token = _read_token(fd) + 1
            payload = f"{token}\n".encode()
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, payload)
            os.fsync(fd)
        except Exception:
            if fd is not None:
                try:
                    _flock_unlock(fd)
                except OSError:
                    pass
                os.close(fd)
            self._release_thread()
            raise
        self._fd = fd
        self.token = token
        # File-lock acquire cannot tell RELEASED from STEALABLE. Incarnation
        # stays 1 so resume after a clean close still replays operations.
        # Steal bumps wait on run_leases rows (rel3_runtime_db).
        self.incarnation = 1
        return token

    def heartbeat(self) -> None:
        if self._fd is None:
            return
        os.fsync(self._fd)
        try:
            os.utime(self.path, None)
        except OSError:
            return

    def release(self) -> None:
        fd = self._fd
        self._fd = None
        self.token = None
        self.incarnation = 1
        if fd is not None:
            try:
                _flock_unlock(fd)
            finally:
                os.close(fd)
        self._release_thread()

    def _release_thread(self) -> None:
        if self._held:
            self._held = False
            self._thread.release()


def _read_token(fd: int) -> int:
    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.read(fd, 64).decode("ascii", errors="replace").strip()
    if not raw:
        return 0
    head = raw.split()[0]
    try:
        return int(head)
    except ValueError:
        return 0


def _flock_exclusive(fd: int) -> None:
    if sys.platform == "win32":
        return
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES}:
            raise RunBusy("run lease busy") from exc
        raise


def _flock_unlock(fd: int) -> None:
    if sys.platform == "win32":
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)
