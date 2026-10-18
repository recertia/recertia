"""Fencing compare against ``run_leases``. Lives under persistence so ledger and graph can share it."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Literal, NoReturn

from recertia.persistence.db import open_db


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
    skill id, and for token-less writes of runs that were never leased: no
    db/table/row is not a live fence. A row that exists — held or released —
    is always checked against the highest token ever issued. A released row
    is not treated as missing.

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
    # Highest token ever issued lives on the row after release. A stale
    # token (or a token-less write) must not land once any holder existed.
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
