"""Copy ``checkpoints.db`` / ``operations.db`` into ``runtime.db`` (REL-3)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from recertia.persistence.db import apply_migrations, open_db
from recertia.persistence.layout import RunsRootLayout
from recertia.persistence.root_lock import RootLock, RootLockBusy
from recertia.runtime.errors import StateCorrupt

IMPORT_ID = "runtime.import_legacy_checkpoints"
_PENDING = {"__pending__": True}
_RUNTIME_MIGRATIONS = Path(__file__).resolve().parent / "migrations" / "runtime"

_LIVE_OPERATIONS_DDL = """
CREATE TABLE operations (
    run_id TEXT NOT NULL,
    incarnation INTEGER NOT NULL DEFAULT 1,
    attempt_no INTEGER NOT NULL,
    node TEXT NOT NULL,
    op_seq INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    fencing_token INTEGER,
    PRIMARY KEY (run_id, incarnation, attempt_no, node, op_seq)
)
"""

_EVAL_OUTBOX_DDL = """
CREATE TABLE IF NOT EXISTS eval_outbox (
    observation_key TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
)
"""

_RUN_SUMMARIES_DDL = """
CREATE TABLE IF NOT EXISTS run_summaries (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    terminal TEXT,
    updated_at TEXT NOT NULL,
    cost_usd REAL,
    task_class TEXT NOT NULL,
    attempt_no INTEGER,
    arm TEXT,
    created_at TEXT NOT NULL
)
"""

_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    request_hash TEXT NOT NULL DEFAULT '',
    summary_json TEXT NOT NULL DEFAULT '{}',
    workdir TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, run_id)
)
"""


def legacy_import_complete(runtime_db: Path) -> bool:
    """True when ``runtime.db`` recorded ``IMPORT_ID`` in ``schema_migrations``."""

    if not runtime_db.is_file():
        return False
    try:
        conn = sqlite3.connect(f"file:{_uri_path(runtime_db)}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=? LIMIT 1",
            (IMPORT_ID,),
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return row is not None


def legacy_sqlite_pending(layout: RunsRootLayout) -> bool:
    """Leftover files still need import. Kept files after a completed import are not pending."""

    if not (layout.checkpoints_db.is_file() or layout.operations_db.is_file()):
        return False
    return not legacy_import_complete(layout.runtime_db)


def checkpoint_db_candidates(runs_root: Path | str) -> list[Path]:
    """SQLite files that may hold checkpoint rows (runtime first, then legacy)."""

    root = Path(runs_root)
    found: list[Path] = []
    runtime = root / "runtime.db"
    legacy = root / "checkpoints.db"
    if runtime.is_file():
        found.append(runtime)
    if legacy.is_file():
        found.append(legacy)
    return found


def live_checkpoint_db(runs_root: Path | str) -> Path | None:
    """The file currently serving checkpoints, or ``None`` when neither exists."""

    root = Path(runs_root)
    runtime = root / "runtime.db"
    legacy = root / "checkpoints.db"
    if sqlite_has_table(runtime, "checkpoints"):
        return runtime
    if legacy.is_file():
        return legacy
    return None


def sqlite_has_table(path: Path, table: str) -> bool:
    if not path.is_file():
        return False
    try:
        conn = sqlite3.connect(f"file:{_uri_path(path)}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return row is not None


def checkpoint_has_run(db: Path, run_id: str) -> bool:
    if not sqlite_has_table(db, "checkpoints"):
        return False
    try:
        conn = sqlite3.connect(f"file:{_uri_path(db)}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM checkpoints WHERE run_id=? LIMIT 1",
            (run_id,),
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return row is not None


def _import_lock_timeout_s() -> float:
    raw = os.environ.get("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "5")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 5.0


def import_legacy_runtime(
    layout: RunsRootLayout, *, timeout_s: float | None = None
) -> bool:
    """Copy legacy SQLite files into ``runtime.db``.

    Returns ``True`` when the dest is ready (imported, already done, or nothing
    to import). A busy exclusive root lock is waited out for ``timeout_s``
    (default ``RECERTIA_IMPORT_LOCK_TIMEOUT_S``, 5s) and then raised as
    :class:`RootLockBusy` — the caller must not write somewhere else.
    Conflicting rows raise :class:`StateCorrupt` and leave the legacy files
    in place.
    """

    if not legacy_sqlite_pending(layout):
        return True
    wait = _import_lock_timeout_s() if timeout_s is None else max(0.0, timeout_s)
    lock = RootLock(layout.parent.root_lock)
    try:
        lock.acquire_exclusive(timeout_s=wait)
    except RootLockBusy as exc:
        raise RootLockBusy(
            f"cannot import leftover runtime files while the root lock is held: {lock.path}"
        ) from exc
    try:
        return _import_under_lock(layout)
    finally:
        lock.release()


def _import_under_lock(layout: RunsRootLayout) -> bool:
    dest_path = layout.runtime_db
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest: sqlite3.Connection | None = open_db(dest_path)
    try:
        assert dest is not None
        apply_migrations(dest, _RUNTIME_MIGRATIONS)
        _ensure_live_operations(dest)
        _ensure_checkpoint_extras(dest)
        dest.execute(_RUNS_DDL)
        dest.commit()
        if not _import_recorded(dest):
            tenant = str(layout.tenant) if layout.tenant is not None else "local"
            dest.execute("BEGIN IMMEDIATE")
            try:
                if layout.checkpoints_db.is_file():
                    _copy_checkpoints(dest, layout.checkpoints_db)
                    _copy_optional_table(dest, layout.checkpoints_db, "eval_outbox")
                    _copy_optional_table(dest, layout.checkpoints_db, "run_summaries")
                    _insert_runs_from_checkpoints(dest, tenant)
                if layout.operations_db.is_file():
                    _copy_operations(dest, layout.operations_db)
                dest.execute(
                    "INSERT INTO schema_migrations(version, checksum, applied_at) VALUES (?, ?, ?)",
                    (IMPORT_ID, IMPORT_ID, datetime.now(timezone.utc).isoformat()),
                )
                dest.commit()
            except Exception:
                dest.execute("ROLLBACK")
                raise
    finally:
        if dest is not None:
            dest.close()
    _retire_legacy_files(layout)
    return True


def _import_recorded(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version=? LIMIT 1",
        (IMPORT_ID,),
    ).fetchone()
    return row is not None


def _ensure_live_operations(conn: sqlite3.Connection) -> None:
    """Create or rebuild ``operations`` with incarnation in the primary key.

    Same crash-safe transaction as ``OperationLedger`` (#87): leftover
    ``operations_rel3`` is dropped or renamed, and Python 3.11 does not
    autocommit DDL (``isolation_level=None``). Caller already holds the
    exclusive root lock.
    """

    if _live_operations_ready(conn):
        return
    _rebuild_live_operations(conn)


def _live_operations_ready(conn: sqlite3.Connection) -> bool:
    names = _table_names(conn)
    if "operations_rel3" in names:
        return False
    if "operations" not in names:
        return False
    return _operations_pk(conn) == ["run_id", "incarnation", "attempt_no", "node", "op_seq"]


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _operations_pk(conn: sqlite3.Connection) -> list[str]:
    info = list(conn.execute("PRAGMA table_info(operations)"))
    return [str(row[1]) for row in sorted((r for r in info if r[5]), key=lambda r: r[5])]


def _rebuild_live_operations(conn: sqlite3.Connection) -> None:
    previous = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            names = _table_names(conn)
            if "operations_rel3" in names and "operations" not in names:
                conn.execute("ALTER TABLE operations_rel3 RENAME TO operations")
                names = _table_names(conn)
            elif "operations_rel3" in names:
                conn.execute("DROP TABLE operations_rel3")
                names = _table_names(conn)
            if "operations" not in names:
                conn.execute(_LIVE_OPERATIONS_DDL)
                conn.execute("COMMIT")
                return
            if _operations_pk(conn) == ["run_id", "incarnation", "attempt_no", "node", "op_seq"]:
                conn.execute("COMMIT")
                return
            info = list(conn.execute("PRAGMA table_info(operations)"))
            cols = {str(row[1]) for row in info}
            incarnation = "incarnation" if "incarnation" in cols else "1"
            fencing = "fencing_token" if "fencing_token" in cols else "NULL"
            conn.execute(_LIVE_OPERATIONS_DDL.replace("operations", "operations_rel3", 1))
            conn.execute(
                f"""
                INSERT INTO operations_rel3 (
                    run_id, incarnation, attempt_no, node, op_seq, result_json, fencing_token
                )
                SELECT run_id, {incarnation}, attempt_no, node, op_seq, result_json, {fencing}
                FROM operations
                """
            )
            conn.execute("DROP TABLE operations")
            conn.execute("ALTER TABLE operations_rel3 RENAME TO operations")
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    finally:
        conn.isolation_level = previous


def _ensure_checkpoint_extras(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "checkpoints", "fencing_token", "INTEGER")
    conn.execute(_EVAL_OUTBOX_DDL)
    conn.execute(_RUN_SUMMARIES_DDL)
    conn.commit()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    names = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in names:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _copy_checkpoints(dest: sqlite3.Connection, src_path: Path) -> None:
    src = open_db(src_path)
    try:
        if not _has_table(src, "checkpoints"):
            return
        src_cols = _columns(src, "checkpoints")
        select = [
            "run_id",
            "seq",
            "node",
            "next_node",
            "state_json",
            "created_at",
            "fencing_token" if "fencing_token" in src_cols else "NULL AS fencing_token",
        ]
        rows = src.execute(
            "SELECT " + ", ".join(select) + " FROM checkpoints"
        ).fetchall()
    finally:
        src.close()
    for run_id, seq, node, next_node, state_json, created_at, fencing_token in rows:
        existing = dest.execute(
            "SELECT node, next_node, state_json, fencing_token FROM checkpoints "
            "WHERE run_id=? AND seq=?",
            (run_id, seq),
        ).fetchone()
        incoming = (node, next_node, state_json, fencing_token)
        if existing is not None:
            if _row_equal(existing, incoming):
                continue
            raise StateCorrupt(
                f"checkpoint conflict during import ({run_id!r}, seq={seq})",
                details={"run_id": run_id, "seq": seq},
            )
        dest.execute(
            "INSERT INTO checkpoints ("
            "run_id, seq, node, next_node, state_json, created_at, fencing_token"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, seq, node, next_node, state_json, created_at, fencing_token),
        )


def _copy_operations(dest: sqlite3.Connection, src_path: Path) -> None:
    src = open_db(src_path)
    try:
        if not _has_table(src, "operations"):
            return
        cols = _columns(src, "operations")
        incarnation = "incarnation" if "incarnation" in cols else "1 AS incarnation"
        fencing = "fencing_token" if "fencing_token" in cols else "NULL AS fencing_token"
        rows = src.execute(
            "SELECT run_id, "
            + incarnation
            + ", attempt_no, node, op_seq, result_json, "
            + fencing
            + " FROM operations"
        ).fetchall()
    finally:
        src.close()
    for run_id, incarnation, attempt_no, node, op_seq, result_json, fencing_token in rows:
        if _is_pending_json(result_json):
            continue
        existing = dest.execute(
            "SELECT result_json, fencing_token FROM operations "
            "WHERE run_id=? AND incarnation=? AND attempt_no=? AND node=? AND op_seq=?",
            (run_id, incarnation, attempt_no, node, op_seq),
        ).fetchone()
        if existing is not None:
            if _row_equal(existing, (result_json, fencing_token)):
                continue
            raise StateCorrupt(
                "operation conflict during import",
                details={
                    "run_id": run_id,
                    "incarnation": incarnation,
                    "attempt_no": attempt_no,
                    "node": node,
                    "op_seq": op_seq,
                },
            )
        dest.execute(
            "INSERT INTO operations ("
            "run_id, incarnation, attempt_no, node, op_seq, result_json, fencing_token"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, incarnation, attempt_no, node, op_seq, result_json, fencing_token),
        )


def _copy_optional_table(dest: sqlite3.Connection, src_path: Path, table: str) -> None:
    src = open_db(src_path)
    try:
        if not _has_table(src, table):
            return
        dest_cols = _columns(dest, table)
        src_cols = _columns(src, table)
        cols = [c for c in dest_cols if c in src_cols]
        if not cols:
            return
        quoted = ", ".join(cols)
        rows = src.execute(f"SELECT {quoted} FROM {table}").fetchall()
        pk = _primary_key(dest, table)
    finally:
        src.close()
    placeholders = ", ".join("?" for _ in cols)
    pk_idx = [cols.index(name) for name in pk if name in cols]
    for row in rows:
        if pk_idx:
            where = " AND ".join(f"{cols[i]}=?" for i in pk_idx)
            existing = dest.execute(
                f"SELECT {quoted} FROM {table} WHERE {where}",
                tuple(row[i] for i in pk_idx),
            ).fetchone()
            if existing is not None:
                continue
        dest.execute(f"INSERT INTO {table} ({quoted}) VALUES ({placeholders})", row)


def _insert_runs_from_checkpoints(dest: sqlite3.Connection, tenant_id: str) -> None:
    rows = dest.execute(
        """
        SELECT c.run_id, c.state_json, c.created_at
        FROM checkpoints c
        JOIN (
            SELECT run_id, MAX(seq) AS seq FROM checkpoints GROUP BY run_id
        ) latest ON c.run_id = latest.run_id AND c.seq = latest.seq
        """
    ).fetchall()
    now = datetime.now(timezone.utc).isoformat()
    for run_id, state_json, created_at in rows:
        status = _status_from_state(state_json)
        dest.execute(
            """
            INSERT INTO runs (
                tenant_id, run_id, status, request_hash, summary_json,
                workdir, created_at, updated_at
            ) VALUES (?, ?, ?, '', '{}', NULL, ?, ?)
            ON CONFLICT(tenant_id, run_id) DO NOTHING
            """,
            (tenant_id, run_id, status, created_at or now, now),
        )


def _status_from_state(state_json: str) -> str:
    try:
        payload = json.loads(state_json)
    except json.JSONDecodeError:
        return "running"
    if not isinstance(payload, dict):
        return "running"
    terminal = payload.get("terminal")
    if terminal in (None, ""):
        return "running"
    return str(terminal)


def _is_pending_json(raw: object) -> bool:
    if not isinstance(raw, str):
        return False
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and parsed == _PENDING


def _retire_legacy_files(layout: RunsRootLayout) -> None:
    _retire_sqlite(layout.checkpoints_db, layout.runs_root / "checkpoints.db.migrated")
    _retire_sqlite(layout.operations_db, layout.runs_root / "operations.db.migrated")


def _retire_sqlite(src: Path, dest: Path) -> None:
    if not src.is_file():
        return
    conn = sqlite3.connect(src)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    if not dest.exists():
        shutil.copy2(src, dest)
    # Keep leftover files for migrate=off rollback. Do not rename them away.


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def _primary_key(conn: sqlite3.Connection, table: str) -> list[str]:
    info = list(conn.execute(f"PRAGMA table_info({table})"))
    return [str(row[1]) for row in sorted((r for r in info if r[5]), key=lambda r: r[5])]


def _row_equal(left: object, right: object) -> bool:
    return tuple(left) == tuple(right)  # type: ignore[arg-type]


def _uri_path(path: Path) -> str:
    return path.resolve().as_posix().replace("?", "%3F")
