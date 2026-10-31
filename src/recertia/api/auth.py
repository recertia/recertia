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
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from recertia.errors import SecretUnavailable
from recertia.persistence.db import open_db
from recertia.ports.identity import Principal, tenant_id

_ALLOWED_SCOPES = frozenset(
    {"runs", "blobs", "metrics", "admin", "exec", "promote", "jobs", "platform_admin"}
)

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
    """Failed-auth throttle keyed by client IP and key id, not the secret."""

    def __init__(self, *, max_failures: int = 20, window_s: float = 60.0) -> None:
        self._max = max_failures
        self._window = window_s
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    @staticmethod
    def _bucket(client_ip: str, key_id: str | None) -> str:
        return f"{client_ip}|{key_id or 'anon'}"

    def check(self, *, client_ip: str, key_id: str | None) -> bool:
        """Return False when this IP and key id are over the failure budget."""

        now = time.monotonic()
        key = self._bucket(client_ip, key_id)
        with self._lock:
            stamps = [t for t in self._failures[key] if now - t < self._window]
            self._failures[key] = stamps
            return len(stamps) < self._max

    def record_failure(self, *, client_ip: str, key_id: str | None) -> None:
        with self._lock:
            self._failures[self._bucket(client_ip, key_id)].append(time.monotonic())


_CURRENT_PEPPER = "v1"
_AUDIT_CAP = 10_000


def _hkdf_sha256(ikm: bytes, *, info: bytes, length: int = 32) -> bytes:
    """HKDF-SHA256 (extract + expand). Used to bind a platform pepper to a tenant."""

    prk = hmac.new(b"\x00" * 32, ikm, hashlib.sha256).digest()
    okm = b""
    block = b""
    counter = 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


class ApiKeyStore:
    """SQLite-backed key registry.

    New keys are HMAC-SHA256 with a pepper. Existing PBKDF2 rows stay valid and
    are rewritten to HMAC on the next successful authenticate.

    Root-lock exception (#159): the store takes no root lock. The API process opens
    it only after taking ``api.lock`` shared, so backup and restore refuse while the
    API runs. The CLI key commands take no lock. They are safe during a backup, which
    copies a consistent snapshot of the file with the SQLite online backup API, but
    they are not excluded from a restore, which overwrites files in place. Do not run
    key commands during a restore.
    """

    _ITERATIONS = 600_000

    def __init__(
        self,
        path: Path | str,
        *,
        pepper: bytes | None = None,
        bind_tenant: bool | None = None,
        max_failures: int = 20,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._limiter = _AuthRateLimiter(max_failures=max_failures)
        self._bind_tenant = self._resolve_bind(bind_tenant)
        self._pepper = self._load_pepper(pepper)
        self._previous_pepper = self._load_previous_pepper()
        with closing(self._connect()) as conn, conn:
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
            self._ensure_hash_columns(conn)
        try:
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise SecretUnavailable(f"could not restrict API key database: {exc}") from exc

    def _connect(self) -> sqlite3.Connection:
        return open_db(self.path)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _resolve_bind(bind_tenant: bool | None) -> bool:
        if bind_tenant is not None:
            return bind_tenant
        from recertia.settings import load_settings

        return load_settings().deployment_mode == "multi_node"

    def _load_pepper(self, explicit: bytes | None) -> bytes:
        if explicit is not None:
            if len(explicit) < 16:
                raise SecretUnavailable("API key pepper is too short")
            return explicit
        if self._bind_tenant:
            from recertia.settings import load_settings

            raw = load_settings().api_key_pepper.strip()
            if len(raw) < 16:
                raise SecretUnavailable("multi_node requires RECERTIA_API_KEY_PEPPER")
            return raw.encode("utf-8")
        path = self.path.parent / "api_key_pepper"
        if path.exists():
            data = path.read_bytes()
            if len(data) < 16:
                raise SecretUnavailable("API key pepper file is unusable")
            return data
        data = secrets.token_bytes(32)
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            data = path.read_bytes()
            if len(data) < 16:
                raise SecretUnavailable("API key pepper file is unusable")
            return data
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        return data

    def _load_previous_pepper(self) -> bytes | None:
        path = self.path.parent / "api_key_pepper.previous"
        if not path.exists():
            return None
        data = path.read_bytes()
        return data if len(data) >= 16 else None

    def _mac_key(self, *, tenant: str, pepper_id: str, pepper: bytes | None = None) -> bytes:
        material = self._pepper if pepper is None else pepper
        if not self._bind_tenant:
            return material
        return _hkdf_sha256(material, info=f"{tenant}|{pepper_id}".encode("utf-8"))

    def _hmac(self, secret: str, *, tenant: str, pepper_id: str, pepper: bytes | None = None) -> bytes:
        key = self._mac_key(tenant=tenant, pepper_id=pepper_id, pepper=pepper)
        return hmac.new(key, secret.encode("utf-8"), hashlib.sha256).digest()

    @staticmethod
    def _ensure_hash_columns(conn: sqlite3.Connection) -> None:
        cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(api_keys)")}
        if "hash_alg" not in cols:
            conn.execute(
                "ALTER TABLE api_keys ADD COLUMN hash_alg TEXT NOT NULL DEFAULT 'pbkdf2-sha256'"
            )
        if "pepper_id" not in cols:
            conn.execute(
                "ALTER TABLE api_keys ADD COLUMN pepper_id TEXT NOT NULL DEFAULT 'legacy'"
            )

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
        secret: str | None = None,
    ) -> None:
        if action == "authenticated" and key_id and self._authenticated_recently(conn, key_id):
            return
        text = detail[:200]
        if secret:
            text = text.replace(secret, "[redacted]")
        conn.execute(
            "INSERT INTO api_key_audit(at, action, key_id, actor, detail) VALUES (?, ?, ?, ?, ?)",
            (self._now(), action, key_id, actor, text),
        )
        conn.execute(
            """DELETE FROM api_key_audit WHERE id NOT IN (
                   SELECT id FROM api_key_audit ORDER BY id DESC LIMIT ?
               )""",
            (_AUDIT_CAP,),
        )

    def _authenticated_recently(self, conn: sqlite3.Connection, key_id: str) -> bool:
        row = conn.execute(
            """SELECT at FROM api_key_audit
               WHERE action='authenticated' AND key_id=?
               ORDER BY id DESC LIMIT 1""",
            (key_id,),
        ).fetchone()
        if row is None:
            return False
        try:
            at = datetime.fromisoformat(str(row[0]))
        except ValueError:
            return False
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - at).total_seconds() < 3600

    def issue(self, *, tenant_id: str, scopes: set[str] | frozenset[str], actor: str) -> IssuedApiKey:
        tenant_id = validate_tenant_id(tenant_id)
        scopes = validate_scopes(scopes)
        key_id = f"key_{secrets.token_hex(8)}"
        token = secrets.token_urlsafe(32)
        secret = f"rec_{key_id}.{token}"
        digest = self._hmac(secret, tenant=tenant_id, pepper_id=_CURRENT_PEPPER)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """INSERT INTO api_keys(
                       key_id, tenant_id, scopes, salt, key_hash, created_at, hash_alg, pepper_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    key_id,
                    tenant_id,
                    ",".join(sorted(scopes)),
                    b"",
                    digest,
                    self._now(),
                    "hmac-sha256",
                    _CURRENT_PEPPER,
                ),
            )
            self._audit(conn, "issued", key_id=key_id, actor=actor, detail=f"tenant={tenant_id}")
        return IssuedApiKey(key_id, secret, tenant_id, scopes)

    def _rehash(self, conn: sqlite3.Connection, *, key_id: str, tenant_id: str, secret: str) -> None:
        digest = self._hmac(secret, tenant=tenant_id, pepper_id=_CURRENT_PEPPER)
        conn.execute(
            """UPDATE api_keys
               SET key_hash=?, salt=?, hash_alg='hmac-sha256', pepper_id=?
               WHERE key_id=?""",
            (digest, b"", _CURRENT_PEPPER, key_id),
        )

    def _hash_matches(
        self,
        conn: sqlite3.Connection,
        *,
        key_id: str,
        tenant_id: str,
        secret: str,
        salt: bytes,
        stored_hash: bytes,
        hash_alg: str,
        pepper_id: str,
    ) -> bool:
        if hash_alg == "pbkdf2-sha256":
            if not hmac.compare_digest(self._hash(secret, salt), stored_hash):
                return False
            self._rehash(conn, key_id=key_id, tenant_id=tenant_id, secret=secret)
            return True
        current = self._hmac(secret, tenant=tenant_id, pepper_id=pepper_id or _CURRENT_PEPPER)
        if hmac.compare_digest(current, stored_hash):
            if pepper_id != _CURRENT_PEPPER:
                self._rehash(conn, key_id=key_id, tenant_id=tenant_id, secret=secret)
            return True
        if self._previous_pepper is not None:
            previous = self._hmac(
                secret,
                tenant=tenant_id,
                pepper_id=pepper_id or _CURRENT_PEPPER,
                pepper=self._previous_pepper,
            )
            if hmac.compare_digest(previous, stored_hash):
                self._rehash(conn, key_id=key_id, tenant_id=tenant_id, secret=secret)
                return True
        return False

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
        hash_alg: str,
        pepper_id: str,
    ) -> Principal | None:
        if not self._hash_matches(
            conn,
            key_id=key_id,
            tenant_id=tenant_id,
            secret=secret,
            salt=salt,
            stored_hash=stored_hash,
            hash_alg=hash_alg,
            pepper_id=pepper_id,
        ):
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
                secret=secret,
            )
            return None
        self._audit(
            conn,
            "authenticated",
            key_id=key_id,
            actor=key_id,
            detail="success",
            secret=secret,
        )
        return Principal.from_api_key(key_id=key_id, tenant_id=tenant_id, scopes=scope_set)

    def authenticate(self, secret: str | None, *, client_ip: str = "unknown") -> Principal | None:
        if not secret:
            return None
        structured = _STRUCTURED_SECRET_RE.fullmatch(secret)
        key_id = structured.group(1) if structured else None
        if not self._limiter.check(client_ip=client_ip, key_id=key_id):
            return None
        if not structured:
            self._limiter.record_failure(client_ip=client_ip, key_id=None)
            with closing(self._connect()) as conn, conn:
                self._audit(
                    conn,
                    "authentication_failed",
                    key_id=None,
                    actor="anonymous",
                    detail="unstructured key rejected",
                    secret=secret,
                )
            return None

        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                """SELECT key_id, tenant_id, scopes, salt, key_hash, hash_alg, pepper_id
                   FROM api_keys WHERE key_id=? AND revoked_at IS NULL""",
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
                    hash_alg=row[5] or "pbkdf2-sha256",
                    pepper_id=row[6] or "legacy",
                )
                if principal is not None:
                    return principal
            self._limiter.record_failure(client_ip=client_ip, key_id=key_id)
            self._audit(
                conn,
                "authentication_failed",
                key_id=key_id,
                actor="anonymous",
                detail="invalid key",
                secret=secret,
            )
        return None

    def revoke(self, key_id: str, *, actor: str) -> bool:
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "UPDATE api_keys SET revoked_at=?, revoked_by=? WHERE key_id=? AND revoked_at IS NULL",
                (self._now(), actor, key_id),
            )
            if cursor.rowcount:
                self._audit(conn, "revoked", key_id=key_id, actor=actor, detail="revoked")
                return True
        return False

    def list_keys(self) -> list[dict[str, str | bool]]:
        with closing(self._connect()) as conn, conn:
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


def _optional_api_key_header() -> Any:
    """Header extractor when FastAPI is installed; a plain default otherwise."""

    try:
        from fastapi import Header
    except ImportError:
        return None
    return Header(default=None, alias="X-API-Key")


def require_key(
    required_scope: str,
    store: ApiKeyStore,
    x_api_key: str | None = _optional_api_key_header(),
    *,
    client_ip: str = "unknown",
) -> Principal:
    from fastapi import HTTPException

    principal = store.authenticate(x_api_key, client_ip=client_ip)
    if principal is None:
        raise HTTPException(status_code=401, detail="invalid API key")
    if not principal.has_scope(required_scope):
        raise HTTPException(status_code=403, detail=f"missing scope: {required_scope}")
    return principal


def require_scope(scope: str, store: ApiKeyStore) -> Callable[..., Principal]:
    """FastAPI dependency factory preserving the header parameter signature."""

    from fastapi import Header, Request

    # Nested annotations resolve against this module, not the enclosing function.
    globals()["Request"] = Request

    def dependency(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> Principal:
        host = request.client.host if request.client is not None else "unknown"
        return require_key(scope, store, x_api_key, client_ip=host)

    return dependency
