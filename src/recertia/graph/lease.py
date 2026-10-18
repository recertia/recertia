"""One flock owner per run. ``run_leases`` rows distinguish release from steal."""

from __future__ import annotations

import errno
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from recertia.persistence.db import open_db

_THREAD_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()

_LEASES_DDL = """
CREATE TABLE IF NOT EXISTS run_leases (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    incarnation INTEGER NOT NULL,
    heartbeat_at TEXT NOT NULL,
    lock_path TEXT,
    PRIMARY KEY (tenant_id, run_id)
)
"""


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

    def __init__(
        self,
        path: Path,
        *,
        db_path: Path | None = None,
        tenant_id: str = "local",
        run_id: str | None = None,
        owner_id: str | None = None,
    ) -> None:
        self.path = path
        self.token: int | None = None
        self.incarnation: int = 1
        self._db_path = Path(db_path) if db_path is not None else None
        self._tenant_id = tenant_id
        self._run_id = run_id
        self._owner_id = owner_id or f"pid:{os.getpid()}"
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
            token, incarnation = self._record_acquire(token)
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
        self.incarnation = incarnation
        return token

    def heartbeat(self) -> None:
        if self._fd is None:
            return
        os.fsync(self._fd)
        try:
            os.utime(self.path, None)
        except OSError:
            return
        self._touch_row()

    def release(self) -> None:
        self._clear_row()
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

    def _record_acquire(self, token: int) -> tuple[int, int]:
        """Held row + free flock is a steal (incarnation bumps). No row is a clean resume."""

        if self._db_path is None or not self._run_id:
            return token, 1
        conn = open_db(self._db_path)
        try:
            conn.execute(_LEASES_DDL)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT fencing_token, incarnation FROM run_leases "
                "WHERE tenant_id=? AND run_id=?",
                (self._tenant_id, self._run_id),
            ).fetchone()
            incarnation = 1
            if row is not None:
                token = max(token, int(row[0]) + 1)
                incarnation = int(row[1]) + 1
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO run_leases (
                    tenant_id, run_id, owner_id, fencing_token, incarnation,
                    heartbeat_at, lock_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, run_id) DO UPDATE SET
                    owner_id=excluded.owner_id,
                    fencing_token=excluded.fencing_token,
                    incarnation=excluded.incarnation,
                    heartbeat_at=excluded.heartbeat_at,
                    lock_path=excluded.lock_path
                """,
                (
                    self._tenant_id,
                    self._run_id,
                    self._owner_id,
                    token,
                    incarnation,
                    now,
                    str(self.path),
                ),
            )
            conn.commit()
            return token, incarnation
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _clear_row(self) -> None:
        if self._db_path is None or not self._run_id:
            return
        conn = open_db(self._db_path)
        try:
            conn.execute(_LEASES_DDL)
            conn.execute(
                "DELETE FROM run_leases WHERE tenant_id=? AND run_id=?",
                (self._tenant_id, self._run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _touch_row(self) -> None:
        if self._db_path is None or not self._run_id or self.token is None:
            return
        conn = open_db(self._db_path)
        try:
            conn.execute(
                "UPDATE run_leases SET heartbeat_at=? WHERE tenant_id=? AND run_id=?",
                (datetime.now(timezone.utc).isoformat(), self._tenant_id, self._run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _release_thread(self) -> None:
        if self._held:
            self._held = False
            self._thread.release()


def require_lease_token(
    db_path: Path,
    *,
    tenant_id: str,
    run_id: str,
    fencing_token: int,
) -> None:
    """Raise ``LeaseLost`` when the caller is not the current ``run_leases`` holder."""

    from recertia.runtime.errors import LeaseLost

    path = Path(db_path)
    if not path.exists():
        return
    conn = open_db(path)
    try:
        names = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "run_leases" not in names:
            return
        row = conn.execute(
            "SELECT fencing_token FROM run_leases WHERE tenant_id=? AND run_id=?",
            (tenant_id, run_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None or int(row[0]) != int(fencing_token):
        raise LeaseLost(
            "lease lost",
            run_id=run_id,
            details={"tenant_id": tenant_id, "fencing_token": fencing_token},
        )


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
