"""One flock owner per run. ``run_leases`` rows persist incarnation and tokens."""

from __future__ import annotations

import errno
import os
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from recertia.errors import Busy
from recertia.persistence.db import open_db
from recertia.persistence.fence import attach_lease_schema, lease_paths_equal, require_lease_token

__all__ = [
    "RunBusy",
    "RunLease",
    "attach_lease_schema",
    "lease_paths_equal",
    "require_lease_token",
]

_THREAD_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()
DEFAULT_LEASE_TTL_S = 30.0

_LEASES_DDL = """
CREATE TABLE IF NOT EXISTS run_leases (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    incarnation INTEGER NOT NULL,
    heartbeat_at TEXT NOT NULL,
    heartbeat_mono REAL NOT NULL DEFAULT 0,
    lock_path TEXT,
    lock_dev INTEGER,
    lock_ino INTEGER,
    lock_gen TEXT,
    status TEXT NOT NULL DEFAULT 'held',
    PRIMARY KEY (tenant_id, run_id)
)
"""

_LEASE_COLUMNS = (
    ("heartbeat_mono", "REAL NOT NULL DEFAULT 0"),
    ("lock_dev", "INTEGER"),
    ("lock_ino", "INTEGER"),
    ("lock_gen", "TEXT"),
    ("status", "TEXT NOT NULL DEFAULT 'held'"),
)


class RunBusy(Busy, RuntimeError):
    """This run is busy, so it can't be started or resumed now.

    ``RunLease.acquire`` raises it in three places: when another thread of this process
    holds the lease (the per-path thread lock), when a ``run_leases`` row is still
    ``held`` by an owner whose heartbeat has not expired, and, off Windows only, when
    another process holds the lock file's ``flock`` (``_flock_exclusive`` is a no-op on
    Windows). The run service also raises it without any lease: ``resume`` when the
    run's queue row is still queued, claimed or running, and a create's sync wait
    (``_enqueue_run``) when the run ended ``run_busy``.

    It is a ``Busy`` error: ``code = "run_busy"``, 409, retryable, CLI exit 6.
    ``RuntimeError`` stays a base for existing callers. A ``RunBusy`` that escapes a
    route other than run start and resume is sent with its own message, so the message
    must name no path. ``tests/unit/test_api_busy_codes.py`` pins this for each raise in
    ``RunLease.acquire``; a new raise site is held to the same rule.
    """

    code = "run_busy"


def _thread_lock(key: str) -> threading.Lock:
    with _GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


def _lease_ttl_s(ttl_s: float | None) -> float:
    if ttl_s is not None:
        return float(ttl_s)
    raw = os.environ.get("RECERTIA_LEASE_TTL_S")
    if raw:
        return float(raw)
    return DEFAULT_LEASE_TTL_S


_SQLITE_INT_MIN, _SQLITE_INT_MAX = -(2**63), 2**63 - 1


def _sqlite_int(value: int) -> int | str:
    """``st_dev`` / ``st_ino`` for the diagnostic ``run_leases`` columns.

    SQLite integers are signed 64-bit. On Windows ``st_ino`` is the file ID, which can
    be larger (an unsigned 64-bit index, or 128 bits on ReFS), and binding it raised
    ``OverflowError`` on every lease. A value out of range is stored as hex text, which
    keeps it exact; INTEGER affinity leaves non-decimal text as text.
    """

    value = int(value)
    if _SQLITE_INT_MIN <= value <= _SQLITE_INT_MAX:
        return value
    return f"{value:#x}"


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
        ttl_s: float | None = None,
    ) -> None:
        self.path = path
        self.token: int | None = None
        self.incarnation: int = 1
        self._db_path = Path(db_path) if db_path is not None else None
        self._tenant_id = tenant_id
        self._run_id = run_id
        self._owner_id = owner_id or f"pid:{os.getpid()}"
        self._ttl_s = _lease_ttl_s(ttl_s)
        self._fd: int | None = None
        self._held = False
        self._thread = _thread_lock(str(path))

    def acquire(self) -> int:
        if not self._thread.acquire(blocking=False):
            raise RunBusy("run lease busy")
        self._held = True
        fd: int | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            path_existed = self.path.is_file()
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            _flock_exclusive(fd)
            prior_token, prior_gen = _read_lock_payload(fd)
            token = prior_token + 1
            st = os.fstat(fd)
            lock_gen = secrets.token_hex(16)
            token, incarnation = self._record_acquire(
                token,
                lock_dev=_sqlite_int(st.st_dev),
                lock_ino=_sqlite_int(st.st_ino),
                lock_gen=lock_gen,
                prior_gen=prior_gen,
                path_existed=path_existed,
            )
            payload = f"{token} {lock_gen}\n".encode()
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
        self._mark_released()
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

    def _record_acquire(
        self,
        token: int,
        *,
        lock_dev: int | str,
        lock_ino: int | str,
        lock_gen: str,
        prior_gen: str | None,
        path_existed: bool,
    ) -> tuple[int, int]:
        """Resume after crash keeps incarnation. Steal (expired + new lock file) bumps it."""

        if self._db_path is None or not self._run_id:
            return token, 1
        conn = open_db(self._db_path)
        try:
            _ensure_leases_table(conn)
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT fencing_token, incarnation, heartbeat_at, heartbeat_mono, "
                "lock_gen, status FROM run_leases "
                "WHERE tenant_id=? AND run_id=?",
                (self._tenant_id, self._run_id),
            ).fetchone()
            incarnation = 1
            if row is not None:
                token = max(token, int(row[0]) + 1)
                incarnation = int(row[1])
                status = str(row[5] or "held")
                stored_gen = None if row[4] is None else str(row[4])
                if status == "held":
                    resume = bool(path_existed and prior_gen and stored_gen and prior_gen == stored_gen)
                    if resume:
                        # Same lock file, flock succeeded: previous holder died. Resume.
                        incarnation = int(row[1])
                    elif not self._expired(str(row[2]), None if row[3] is None else float(row[3])):
                        raise RunBusy("run lease busy")
                    else:
                        incarnation = int(row[1]) + 1
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO run_leases (
                    tenant_id, run_id, owner_id, fencing_token, incarnation,
                    heartbeat_at, heartbeat_mono, lock_path, lock_dev, lock_ino,
                    lock_gen, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'held')
                ON CONFLICT(tenant_id, run_id) DO UPDATE SET
                    owner_id=excluded.owner_id,
                    fencing_token=excluded.fencing_token,
                    incarnation=excluded.incarnation,
                    heartbeat_at=excluded.heartbeat_at,
                    heartbeat_mono=excluded.heartbeat_mono,
                    lock_path=excluded.lock_path,
                    lock_dev=excluded.lock_dev,
                    lock_ino=excluded.lock_ino,
                    lock_gen=excluded.lock_gen,
                    status='held'
                """,
                (
                    self._tenant_id,
                    self._run_id,
                    self._owner_id,
                    token,
                    incarnation,
                    now,
                    time.monotonic(),
                    str(self.path),
                    lock_dev,
                    lock_ino,
                    lock_gen,
                ),
            )
            conn.commit()
            return token, incarnation
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _mark_released(self) -> None:
        if self._db_path is None or not self._run_id:
            return
        conn = open_db(self._db_path)
        try:
            _ensure_leases_table(conn)
            conn.execute(
                """
                UPDATE run_leases SET
                    status='released',
                    lock_dev=NULL,
                    lock_ino=NULL
                WHERE tenant_id=? AND run_id=?
                """,
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
                """
                UPDATE run_leases
                SET heartbeat_at=?, heartbeat_mono=?
                WHERE tenant_id=? AND run_id=? AND fencing_token=?
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    time.monotonic(),
                    self._tenant_id,
                    self._run_id,
                    self.token,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _expired(self, heartbeat_at: str, heartbeat_mono: float | None) -> bool:
        now_wall = datetime.now(timezone.utc)
        try:
            then = datetime.fromisoformat(heartbeat_at)
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
            if (now_wall - then).total_seconds() >= self._ttl_s:
                return True
        except ValueError:
            return True
        if heartbeat_mono is None:
            return False
        stored = heartbeat_mono
        now_mono = time.monotonic()
        if now_mono < stored:
            return True
        return (now_mono - stored) >= self._ttl_s

    def _release_thread(self) -> None:
        if self._held:
            self._held = False
            self._thread.release()


def _ensure_leases_table(conn: sqlite3.Connection) -> None:
    conn.execute(_LEASES_DDL)
    names = {str(row[1]) for row in conn.execute("PRAGMA table_info(run_leases)")}
    for column, decl in _LEASE_COLUMNS:
        if column not in names:
            conn.execute(f"ALTER TABLE run_leases ADD COLUMN {column} {decl}")


def _read_lock_payload(fd: int) -> tuple[int, str | None]:
    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.read(fd, 128).decode("ascii", errors="replace").strip()
    if not raw:
        return 0, None
    parts = raw.split()
    try:
        token = int(parts[0])
    except ValueError:
        token = 0
    gen = parts[1] if len(parts) > 1 else None
    return token, gen


def _read_token(fd: int) -> int:
    token, _gen = _read_lock_payload(fd)
    return token


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
