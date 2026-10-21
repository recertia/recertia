"""Operator-only promotion audit store (REC-009, Gap A).

A golden-gate refusal records its full detail here: every failing fixture id, golden
path, result row and run detail. Proposers never see any of it. They get a
decision-only :class:`~recertia.review.promotion.PromotionError` and an opaque
``audit_ref``, never a path.

The store is a SQLite file under an operator root set by ``RECERTIA_OPERATOR_AUDIT_ROOT``.
It fails closed:

* an unset root raises :class:`AuditSinkUnavailable`; there is no default location
  and the detail never falls back to a proposer-readable place;
* a root inside a git checkout, or inside any caller-supplied forbidden root (the
  API root, which holds every tenant ``runs_root``, or the promote ``runs_root``),
  is refused;
* the root must be a directory owned by the effective user and is set to ``0700``
  (also when it already existed); the database is created ``0600`` with ``O_NOFOLLOW``
  before SQLite opens it, so there is no window with a wider mode. SQLite gives the
  ``-wal``/``-shm`` sidecars the database file's mode; pre-existing sidecars are
  checked and set to ``0600`` as well;
* any ``OSError`` or ``sqlite3.Error`` while opening or writing raises
  :class:`AuditSinkUnavailable` with a fixed message (no path).

File modes do not separate seats that share one OS user. On a shared box treat the
store as readable by every seat and never record real held-out detail there.

Reading is an operator action (:meth:`OperatorAuditStore.read`). No proposer plane
(``recertia.nodes``, ``recertia.distill``, ``recertia.solver``, the
``recertia.jobs.workers`` miner) may import this module; a boundary test enforces it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recertia.persistence.db import open_db

__all__ = [
    "AuditSinkUnavailable",
    "OperatorAuditStore",
    "open_operator_audit",
]

AUDIT_FILENAME = "promotion_audit.sqlite"


_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class AuditSinkUnavailable(Exception):
    """The operator audit store is unset, misplaced, or cannot be opened or written."""


_ENTRY_KEYS = ("skill_id", "version", "refusal_kind", "failing_fixtures", "results", "detail")


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _git_checkout_above(path: Path) -> bool:
    return any((candidate / ".git").exists() for candidate in (path, *path.parents))


def _require_owned(st: os.stat_result, what: str) -> None:
    if st.st_uid != os.geteuid():
        raise AuditSinkUnavailable(f"operator audit {what} is owned by another user")


def _prepare_root(real: Path) -> None:
    """Create or adopt the root: a directory owned by the effective user, mode 0700."""

    try:
        real.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    st = os.lstat(real)
    if not stat.S_ISDIR(st.st_mode):
        raise AuditSinkUnavailable("operator audit root is not a directory")
    _require_owned(st, "root")
    os.chmod(real, 0o700)


def _prepare_db_file(path: Path) -> None:
    """Create the database 0600 before SQLite sees it; adopt an existing one only if safe."""

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise AuditSinkUnavailable("operator audit database is not a regular file")
        _require_owned(st, "database")
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    for suffix in _SIDECAR_SUFFIXES:
        sidecar = path.with_name(path.name + suffix)
        try:
            st = os.lstat(sidecar)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(st.st_mode):
            raise AuditSinkUnavailable("operator audit database sidecar is not a regular file")
        _require_owned(st, "database sidecar")
        os.chmod(sidecar, 0o600)


class OperatorAuditStore:
    """SQLite audit sink under an operator root. Connections are per call.

    Satisfies :class:`recertia.review.promotion.PromotionAuditSink`. The promotion
    module depends only on that protocol, so importing promotion never pulls this
    module into a proposer plane.
    """

    def __init__(
        self,
        root: Path | str | None,
        *,
        forbidden_roots: Iterable[Path | str] = (),
    ) -> None:
        if root is None or not str(root).strip():
            raise AuditSinkUnavailable("operator audit root is unset")
        real = Path(os.path.realpath(Path(root).expanduser()))
        if _git_checkout_above(real):
            raise AuditSinkUnavailable("operator audit root must be outside any git checkout")
        for forbidden in forbidden_roots:
            if _inside(real, Path(os.path.realpath(forbidden))):
                raise AuditSinkUnavailable(
                    "operator audit root must be outside the API root and tenant runs roots"
                )
        self.root = real
        self.path = real / AUDIT_FILENAME
        failed = False
        try:
            _prepare_root(real)
            _prepare_db_file(self.path)
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS promotion_audit (
                            audit_ref TEXT PRIMARY KEY,
                            recorded_at TEXT NOT NULL,
                            skill_id TEXT NOT NULL,
                            version INTEGER NOT NULL,
                            refusal_kind TEXT NOT NULL,
                            payload TEXT NOT NULL
                        )
                        """
                    )
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            failed = True
        if failed:
            # Raised outside the handler: the OS or SQLite error (which names the path)
            # is neither chained nor kept as context.
            raise AuditSinkUnavailable("operator audit store could not be opened")

    def _connect(self) -> Any:
        return open_db(self.path)

    def record(self, entry: Mapping[str, Any]) -> str:
        """Store one refusal; return an opaque ``audit_ref`` (random, not content-derived)."""

        unknown = set(entry) - set(_ENTRY_KEYS)
        if unknown:
            raise ValueError(f"unknown audit entry keys: {sorted(unknown)}")
        audit_ref = f"pa-{uuid.uuid4().hex}"
        payload = {
            "skill_id": str(entry.get("skill_id", "")),
            "version": int(entry.get("version", 0)),
            "refusal_kind": str(entry.get("refusal_kind", "")),
            "failing_fixtures": [str(x) for x in entry.get("failing_fixtures", [])],
            "results": list(entry.get("results", [])),
            "detail": str(entry.get("detail", "")),
        }
        failed = False
        try:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO promotion_audit(audit_ref, recorded_at, skill_id, "
                        "version, refusal_kind, payload) VALUES (?,?,?,?,?,?)",
                        (
                            audit_ref,
                            datetime.now(timezone.utc).isoformat(),
                            payload["skill_id"],
                            payload["version"],
                            payload["refusal_kind"],
                            json.dumps(payload, default=str),
                        ),
                    )
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            failed = True
        if failed:
            raise AuditSinkUnavailable("operator audit store write failed")
        return audit_ref

    def read(self, audit_ref: str) -> dict[str, Any] | None:
        """Operator read. Returns the full record, or ``None`` for an unknown ref."""

        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT recorded_at, payload FROM promotion_audit WHERE audit_ref = ?",
                (audit_ref,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        record = json.loads(row[1])
        record["audit_ref"] = audit_ref
        record["recorded_at"] = row[0]
        return dict(record)


def open_operator_audit(
    root: Path | str | None,
    *,
    forbidden_roots: Iterable[Path | str] = (),
) -> OperatorAuditStore:
    """Open the sink or raise :class:`AuditSinkUnavailable`. There is no fallback.

    Promotion callers (the API route, ``recertia skills promote``) open the sink before
    the golden gate runs and refuse the promotion when this raises.
    """

    return OperatorAuditStore(root, forbidden_roots=forbidden_roots)
