"""Durable, scoped API-key authentication."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Callable

from fastapi import Header, HTTPException

from recertia.ports.identity import Principal, tenant_id

_ALLOWED_SCOPES = frozenset({"runs", "blobs", "metrics", "admin", "exec", "promote", "jobs"})

# New secrets: rec_<key_id>.<token> — enables O(1) lookup by key_id.
_STRUCTURED_SECRET_RE = re.compile(r"^rec_(key_[0-9a-f]+)\.([A-Za-z0-9_-]+)$")


@dataclass(frozen=True)
class IssuedApiKey:
    key_id: str
    secret: str
    tenant_id: str
    scopes: frozenset[str]


def validate_tenant_id(value: str) -> str:
    return str(tenant_id(value))


def validate_scopes(scopes: set[str] | frozenset[str]) -> frozenset[str]:
    normalized = frozenset(scopes)
    if not normalized:
        raise ValueError("at least one scope is required")
    unknown = normalized - _ALLOWED_SCOPES
    if unknown:
        raise ValueError(f"unknown scopes: {sorted(unknown)}")
    if any("," in scope or scope != scope.strip() or not scope for scope in normalized):
        raise ValueError("scopes must be allowlisted tokens without commas or whitespace")
    return normalized


class _AuthRateLimiter:
    """Simple in-process failed-auth throttle (per secret fingerprint)."""

    def __init__(self, *, max_failures: int = 20, window_s: float = 60.0) -> None:
        self._max = max_failures
        self._window = window_s
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def _key(self, secret: str) -> str:
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:32]

    def check(self, secret: str) -> bool:
        """Return False when the caller should be rejected for rate limiting."""

        now = time.monotonic()
        key = self._key(secret)
        with self._lock:
            stamps = [t for t in self._failures[key] if now - t < self._window]
            self._failures[key] = stamps
            return len(stamps) < self._max

    def record_failure(self, secret: str) -> None:
        with self._lock:
            self._failures[self._key(secret)].append(time.monotonic())


class ApiKeyStore:
    """SQLite-backed key registry. Only salted PBKDF2 hashes are persisted."""

    _ITERATIONS = 600_000

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._limiter = _AuthRateLimiter()
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS api_keys (
                    key_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    salt BLOB NOT NULL,
                    key_hash BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT,
                    revoked_by TEXT
                );
                CREATE TABLE IF NOT EXISTS api_key_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    key_id TEXT,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL
                );
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @classmethod
    def _hash(cls, secret: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, cls._ITERATIONS)

    def _audit(
        self,
        conn: sqlite3.Connection,
        action: str,
        *,
        key_id: str | None,
        actor: str,
        detail: str,
    ) -> None:
        conn.execute(
            "INSERT INTO api_key_audit(at, action, key_id, actor, detail) VALUES (?, ?, ?, ?, ?)",
            (self._now(), action, key_id, actor, detail),
        )

    def issue(self, *, tenant_id: str, scopes: set[str] | frozenset[str], actor: str) -> IssuedApiKey:
        tenant_id = validate_tenant_id(tenant_id)
        scopes = validate_scopes(scopes)
        key_id = f"key_{secrets.token_hex(8)}"
        token = secrets.token_urlsafe(32)
        secret = f"rec_{key_id}.{token}"
        salt = secrets.token_bytes(16)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO api_keys(key_id, tenant_id, scopes, salt, key_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    key_id,
                    tenant_id,
                    ",".join(sorted(scopes)),
                    salt,
                    self._hash(secret, salt),
                    self._now(),
                ),
            )
            self._audit(conn, "issued", key_id=key_id, actor=actor, detail=f"tenant={tenant_id}")
        return IssuedApiKey(key_id, secret, tenant_id, scopes)

    def _principal_from_row(
        self,
        conn: sqlite3.Connection,
        *,
        key_id: str,
        tenant_id: str,
        scopes: str,
        salt: bytes,
        stored_hash: bytes,
        secret: str,
    ) -> Principal | None:
        if not hmac.compare_digest(self._hash(secret, salt), stored_hash):
            return None
        try:
            validate_tenant_id(tenant_id)
            scope_set = validate_scopes(set(scopes.split(",")))
        except ValueError:
            self._audit(
                conn,
                "authentication_failed",
                key_id=key_id,
                actor=key_id,
                detail="invalid tenant or scopes",
            )
            return None
        self._audit(conn, "authenticated", key_id=key_id, actor=key_id, detail="success")
        return Principal.from_api_key(key_id=key_id, tenant_id=tenant_id, scopes=scope_set)

    def authenticate(self, secret: str | None) -> Principal | None:
        if not secret:
            return None
        if not self._limiter.check(secret):
            return None

        structured = _STRUCTURED_SECRET_RE.fullmatch(secret)
        if not structured:
            self._limiter.record_failure(secret)
            with self._connect() as conn:
                self._audit(
                    conn,
                    "authentication_failed",
                    key_id=None,
                    actor="anonymous",
                    detail="unstructured key rejected",
                )
            return None

        key_id = structured.group(1)
        with self._connect() as conn:
            row = conn.execute(
                """SELECT key_id, tenant_id, scopes, salt, key_hash FROM api_keys
                   WHERE key_id=? AND revoked_at IS NULL""",
                (key_id,),
            ).fetchone()
            if row is not None:
                principal = self._principal_from_row(
                    conn,
                    key_id=row[0],
                    tenant_id=row[1],
                    scopes=row[2],
                    salt=row[3],
                    stored_hash=row[4],
                    secret=secret,
                )
                if principal is not None:
                    return principal
            self._limiter.record_failure(secret)
            self._audit(
                conn, "authentication_failed", key_id=key_id, actor="anonymous", detail="invalid key"
            )
        return None

    def revoke(self, key_id: str, *, actor: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE api_keys SET revoked_at=?, revoked_by=? WHERE key_id=? AND revoked_at IS NULL",
                (self._now(), actor, key_id),
            )
            if cursor.rowcount:
                self._audit(conn, "revoked", key_id=key_id, actor=actor, detail="revoked")
                return True
        return False

    def list_keys(self) -> list[dict[str, str | bool]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key_id, tenant_id, scopes, created_at, revoked_at FROM api_keys ORDER BY created_at"
            ).fetchall()
        return [
            {
                "key_id": row[0],
                "tenant_id": row[1],
                "scopes": row[2],
                "created_at": row[3],
                "revoked": bool(row[4]),
            }
            for row in rows
        ]


def require_key(
    required_scope: str,
    store: ApiKeyStore,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Principal:
    principal = store.authenticate(x_api_key)
    if principal is None:
        raise HTTPException(status_code=401, detail="invalid API key")
    if required_scope not in principal.scopes and "admin" not in principal.scopes:
        raise HTTPException(status_code=403, detail=f"missing scope: {required_scope}")
    return principal


def require_scope(scope: str, store: ApiKeyStore) -> Callable[..., Principal]:
    """FastAPI dependency factory preserving the header parameter signature."""

    def dependency(
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> Principal:
        return require_key(scope, store, x_api_key)

    return dependency
