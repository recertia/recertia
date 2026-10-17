"""Exclusive flock per resolved workdir (REL-3 F11)."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from recertia.runtime.errors import WorkspaceBusy

_THREAD_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()


def _thread_lock(key: str) -> threading.Lock:
    with _GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


@dataclass
class WorkspaceLockHandle:
    """Held workspace flock. Call :meth:`release` when the owner finishes."""

    lock_path: Path
    workdir: Path
    owner_id: str
    _fd: int | None = field(default=None, repr=False)
    _thread: threading.Lock | None = field(default=None, repr=False)
    _held: bool = field(default=False, repr=False)

    def release(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is not None:
            try:
                _flock_unlock(fd)
            finally:
                os.close(fd)
        if self._held and self._thread is not None:
            self._held = False
            self._thread.release()


class WorkspaceLock:
    """``flock`` under ``locks_dir / workspaces / <sha256(resolved)>.lock``."""

    def __init__(self, locks_dir: Path | str) -> None:
        self.locks_dir = Path(locks_dir)

    def lock_path_for(self, path: Path | str) -> Path:
        resolved = Path(path).resolve()
        digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
        return self.locks_dir / "workspaces" / f"{digest}.lock"

    def acquire(self, path: Path | str, *, owner_id: str) -> WorkspaceLockHandle:
        """Non-blocking exclusive lock. Raises :class:`WorkspaceBusy` when held."""

        resolved = Path(path).resolve()
        lock_path = self.lock_path_for(resolved)
        thread = _thread_lock(str(lock_path))
        if not thread.acquire(blocking=False):
            raise WorkspaceBusy(
                f"workspace busy: {resolved}",
                details={"workdir": str(resolved)},
            )
        fd: int | None = None
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            _flock_exclusive(fd, workdir=resolved)
            payload = json.dumps({"owner_id": owner_id, "workdir": str(resolved)}).encode()
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, payload)
            try:
                os.fsync(fd)
            except OSError:
                pass
        except Exception:
            if fd is not None:
                try:
                    _flock_unlock(fd)
                except OSError:
                    pass
                os.close(fd)
            thread.release()
            raise
        return WorkspaceLockHandle(
            lock_path=lock_path,
            workdir=resolved,
            owner_id=owner_id,
            _fd=fd,
            _thread=thread,
            _held=True,
        )


def _flock_exclusive(fd: int, *, workdir: Path) -> None:
    if sys.platform == "win32":
        return
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES}:
            holder = _read_holder(fd)
            raise WorkspaceBusy(
                f"workspace busy: {workdir}",
                details={
                    "workdir": str(workdir),
                    "holder_owner_id": holder.get("owner_id"),
                },
            ) from exc
        raise


def _flock_unlock(fd: int) -> None:
    if sys.platform == "win32":
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def _read_holder(fd: int) -> dict[str, object]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 4096).decode("utf-8", errors="replace").strip()
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
