"""Durable idempotency keys in ``control.db`` (REL-5 / F20)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from recertia.persistence.db import apply_migrations, open_db
from recertia.runtime.errors import IdempotencyMismatch

_MIGRATIONS = Path(__file__).resolve().parent / "migrations" / "control"


def request_hash(payload: Mapping[str, Any] | str) -> str:
    if isinstance(payload, str):
        blob = payload.encode("utf-8")
    else:
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    return hashlib.sha256(blob).hexdigest()


class IdempotencyStore:
    """One connection to ``root/control.db`` for ``idempotency_keys``."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = open_db(self.path)
        self._conn.isolation_level = None
        self._conn.row_factory = sqlite3.Row
        apply_migrations(self._conn, _MIGRATIONS)

    def close(self) -> None:
        self._conn.close()

    def check(
        self,
        tenant_id: str,
        scope: str,
        key: str,
        request_hash: str,
    ) -> Any | None:
        """Return the stored response, ``None`` if unseen, or raise on hash mismatch."""

        row = self._row(tenant_id, scope, key)
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise IdempotencyMismatch("idempotency key reused with a different request")
        raw = row["response_json"]
        return json.loads(raw) if raw else {}

    def put(
        self,
        tenant_id: str,
        scope: str,
        key: str,
        request_hash: str,
        response: Any,
    ) -> Any:
        """Store ``response``. A racing insert with the same hash returns the stored body."""

        payload = json.dumps(response, default=str)
        stamp = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._row(tenant_id, scope, key)
                if existing is not None:
                    if existing["request_hash"] != request_hash:
                        self._conn.execute("ROLLBACK")
                        raise IdempotencyMismatch(
                            "idempotency key reused with a different request"
                        )
                    self._conn.execute("COMMIT")
                    raw = existing["response_json"]
                    return json.loads(raw) if raw else {}
                self._conn.execute(
                    """
                    INSERT INTO idempotency_keys (
                        tenant_id, scope, key, request_hash, response_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (tenant_id, scope, key, request_hash, payload, stamp),
                )
                self._conn.execute("COMMIT")
                return response
            except IdempotencyMismatch:
                raise
            except sqlite3.IntegrityError:
                self._conn.execute("ROLLBACK")
                replay = self.check(tenant_id, scope, key, request_hash)
                return {} if replay is None else replay
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise

    def _row(self, tenant_id: str, scope: str, key: str) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT request_hash, response_json FROM idempotency_keys
            WHERE tenant_id = ? AND scope = ? AND key = ?
            """,
            (tenant_id, scope, key),
        ).fetchone()
