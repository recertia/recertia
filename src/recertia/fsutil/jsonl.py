"""Locked JSONL append and whole-file rewrite."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from recertia.faults import fault_point
from recertia.fsutil.atomic import Durability, atomic_write_bytes
from recertia.fsutil.locks import file_lock


def iter_complete_lines(path: Path) -> Iterator[bytes]:
    """Yield newline-terminated records. A trailing partial line is dropped."""

    if not path.exists():
        return
    with path.open("rb") as handle:
        data = handle.read()
    if not data:
        return
    if not data.endswith(b"\n"):
        data = data.rsplit(b"\n", 1)[0]
        if not data:
            return
        data += b"\n"
    for line in data.splitlines(keepends=False):
        if line:
            yield line


class JsonlAppender:
    def __init__(
        self,
        path: Path,
        *,
        durability: Durability,
        batch_interval_s: float = 1.0,
        fsync_each: bool = False,
        lock_timeout_s: float = 5.0,
    ) -> None:
        self.path = path
        self.durability = durability
        self.batch_interval_s = batch_interval_s
        self.fsync_each = fsync_each
        self.lock_timeout_s = lock_timeout_s
        self._last_fsync = 0.0
        self._dirty = False

    def _lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".lock")

    def append(self, line: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self._lock_path(), timeout_s=self.lock_timeout_s):
            self.append_unlocked(line)

    def append_unlocked(
        self,
        line: bytes,
        *,
        before_fsync: Callable[[], None] | None = None,
    ) -> None:
        """Append while the caller already holds this file's lock.

        ``file_lock`` is not reentrant. The ledger critical section takes the
        lock itself and calls this so the write stays inside the same hold.
        """

        payload = line if line.endswith(b"\n") else line + b"\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.write(fd, payload)
            fault_point("jsonl.mid_write")
            if before_fsync is not None:
                before_fsync()
            self._dirty = True
            now = time.monotonic()
            should_fsync = self.durability.fsync_file and (
                self.fsync_each or (now - self._last_fsync) >= self.batch_interval_s
            )
            if should_fsync:
                os.fsync(fd)
                self._last_fsync = now
                self._dirty = False
        finally:
            os.close(fd)

    def append_many(self, lines: Sequence[bytes]) -> None:
        for line in lines:
            self.append(line)

    def close(self) -> None:
        if not self._dirty or not self.durability.fsync_file:
            return
        if not self.path.exists():
            return
        with file_lock(self._lock_path(), timeout_s=self.lock_timeout_s):
            fd = os.open(self.path, os.O_RDWR)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        self._dirty = False


class JsonlRewriter:
    """Whole-file rewrite under the same lock the appender uses."""

    def rewrite(self, path: Path, lines: Sequence[bytes], *, durability: Durability) -> None:
        lock_path = path.with_suffix(path.suffix + ".lock")
        with file_lock(lock_path):
            self.rewrite_unlocked(path, lines, durability=durability)

    def rewrite_unlocked(
        self, path: Path, lines: Sequence[bytes], *, durability: Durability
    ) -> None:
        """Rewrite while the caller already holds the JSONL lock.

        ``file_lock`` is not reentrant. ``RevokeQueue.drain`` holds the lock
        across the read and this rewrite so a concurrent append cannot land
        in the window between them.
        """

        body = b"".join(line if line.endswith(b"\n") else line + b"\n" for line in lines)
        atomic_write_bytes(path, body, durability=durability)
