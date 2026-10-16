"""Cross-process ``flock`` with a thread mutex inside it."""

from __future__ import annotations

import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from recertia.runtime.errors import LedgerLockTimeout

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

_THREAD_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()


def _thread_lock(key: str) -> threading.Lock:
    with _GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def file_lock(path: Path, *, timeout_s: float = 5.0) -> Iterator[None]:
    """Exclusive lock on ``path``. The lock file is ``path`` itself.

    Callers that want a sibling lock pass ``path.with_suffix(path.suffix + '.lock')``.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    thread_lock = _thread_lock(str(path))
    if not thread_lock.acquire(timeout=timeout_s):
        raise LedgerLockTimeout(f"timed out waiting for {path}")
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if sys.platform == "win32":
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as exc:
                raise LedgerLockTimeout(f"could not lock {path}: {exc}") from exc
        try:
            yield
        finally:
            if sys.platform == "win32":
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
        thread_lock.release()
