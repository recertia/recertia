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
from typing import Literal, NoReturn

from recertia.persistence.db import open_db

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


class RunBusy(RuntimeError):
    """Another process holds this run's lease."""


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
            raise RunBusy(f"run lease busy: {self.path.name}")
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
                lock_dev=int(st.st_dev),
                lock_ino=int(st.st_ino),
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
        lock_dev: int,
        lock_ino: int,
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


def require_lease_token(
    db_path: Path,
    *,
    tenant_id: str,
    run_id: str,
    fencing_token: int | None,
    conn: sqlite3.Connection | None = None,
    missing_row: Literal["reject", "allow"] = "reject",
) -> None:
    """Raise ``LeaseLost`` when the caller is not the current ``run_leases`` holder.

    Fail closed (``missing_row='reject'``): a missing database, missing
    ``run_leases`` table, missing row, or mismatched/absent token all raise.
    ``missing_row='allow'`` is for hash-chain appends whose ``target`` may be a
    skill id: no db/table/row (or a released row) is not a live fence. A held
    row still requires a matching token.

    When ``conn`` is supplied the check runs on that connection so it can share
    a ``BEGIN IMMEDIATE`` with a write. Attached ``lease`` schemas are searched
    when ``run_leases`` is not in ``main``.
    """

    from recertia.runtime.errors import LeaseLost

    def _reject() -> NoReturn:
        raise LeaseLost(
            "lease lost",
            run_id=run_id,
            details={"tenant_id": tenant_id, "fencing_token": fencing_token},
        )

    if fencing_token is None and missing_row != "allow":
        _reject()
    path = Path(db_path)
    owned = False
    if conn is None:
        if not path.exists():
            if missing_row == "allow":
                return
            _reject()
        conn = open_db(path)
        owned = True
    try:
        schema = _leases_schema(conn)
        if schema is None:
            if missing_row == "allow":
                return
            _reject()
        cols = {
            str(row[1])
            for row in conn.execute(f"PRAGMA {schema}.table_info(run_leases)")
        }
        if "status" in cols:
            row = conn.execute(
                f"SELECT fencing_token, status FROM {schema}.run_leases "
                "WHERE tenant_id=? AND run_id=?",
                (tenant_id, run_id),
            ).fetchone()
        else:
            token_row = conn.execute(
                f"SELECT fencing_token FROM {schema}.run_leases "
                "WHERE tenant_id=? AND run_id=?",
                (tenant_id, run_id),
            ).fetchone()
            row = None if token_row is None else (token_row[0], "held")
    finally:
        if owned:
            conn.close()
    if row is None:
        if missing_row == "allow":
            return
        _reject()
    if str(row[1] or "held") != "held":
        if missing_row == "allow":
            return
        _reject()
    if fencing_token is None or int(row[0]) != int(fencing_token):
        _reject()


def lease_paths_equal(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return os.path.normpath(str(left)) == os.path.normpath(str(right))


def attach_lease_schema(conn: sqlite3.Connection, lease_db: Path) -> None:
    """ATTACH ``lease_db`` as schema ``lease`` when it is not already attached."""

    names = {str(row[1]) for row in conn.execute("PRAGMA database_list")}
    if "lease" in names:
        return
    conn.execute("ATTACH DATABASE ? AS lease", (os.fspath(Path(lease_db).resolve()),))


def _leases_schema(conn: sqlite3.Connection) -> str | None:
    for _seq, name, _file in conn.execute("PRAGMA database_list"):
        schema = str(name)
        if not schema.replace("_", "").isalnum():
            continue
        tables = {
            str(row[0])
            for row in conn.execute(
                f"SELECT name FROM {schema}.sqlite_master WHERE type='table'"
            )
        }
        if "run_leases" in tables:
            return schema
    return None


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
