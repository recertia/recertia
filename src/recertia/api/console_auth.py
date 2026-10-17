"""Human console auth: optional OIDC + explicit-opt-in dev login.

API keys remain for automation. Browser sessions carry role + tenant membership.
Default mode is ``off`` (no browser sessions). Dev login is a second flag on top
of ``RECERTIA_CONSOLE_AUTH=dev``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Literal, TypedDict
from urllib.parse import urlencode

from fastapi import Header, HTTPException, Request

from recertia.api.auth import validate_tenant_id

Role = Literal["operator", "reviewer", "admin"]
_VALID_ROLES = frozenset({"operator", "reviewer", "admin"})
_OIDC_TTL_S = 600


def auth_mode() -> str:
    return os.environ.get("RECERTIA_CONSOLE_AUTH", "off").strip().lower() or "off"


def dev_login_enabled() -> bool:
    if auth_mode() not in {"dev", "development"}:
        return False
    flag = os.environ.get("RECERTIA_CONSOLE_DEV_LOGIN", "").strip().lower()
    return flag in {"1", "true", "yes"}


def dev_admin_enabled() -> bool:
    flag = os.environ.get("RECERTIA_CONSOLE_DEV_ADMIN", "").strip().lower()
    return flag in {"1", "true", "yes"}


def cookie_secure() -> bool:
    raw = os.environ.get("RECERTIA_CONSOLE_COOKIE_SECURE", "").strip().lower()
    if raw in {"0", "false", "no"}:
        return False
    if raw in {"1", "true", "yes"}:
        return True
    return auth_mode() != "dev"


class SessionCookieKwargs(TypedDict):
    httponly: bool
    samesite: Literal["lax", "strict", "none"]
    secure: bool
    path: str


def session_cookie_kwargs() -> SessionCookieKwargs:
    return {
        "httponly": True,
        "samesite": "strict",
        "secure": cookie_secure(),
        "path": "/",
    }


def csrf_cookie_kwargs() -> SessionCookieKwargs:
    """Double-submit cookie. Not HttpOnly, so the console can copy it into a header."""

    base = session_cookie_kwargs()
    return {
        "httponly": False,
        "samesite": base["samesite"],
        "secure": base["secure"],
        "path": base["path"],
    }


def resolve_session_secret(explicit: str | None = None) -> bytes:
    raw = (explicit or os.environ.get("RECERTIA_CONSOLE_SESSION_SECRET") or "").strip()
    if raw:
        if len(raw) < 32:
            raise RuntimeError("RECERTIA_CONSOLE_SESSION_SECRET must be at least 32 characters")
        return raw.encode()
    if auth_mode() == "oidc":
        raise RuntimeError(
            "RECERTIA_CONSOLE_SESSION_SECRET is required when RECERTIA_CONSOLE_AUTH=oidc"
        )
    return secrets.token_hex(32).encode()


@dataclass(frozen=True)
class ConsoleUser:
    user_id: str
    display_name: str
    roles: frozenset[str]
    tenants: tuple[str, ...]
    active_tenant: str

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles

    def may(self, role: Role) -> bool:
        if self.is_admin:
            return True
        order = {"operator": 1, "reviewer": 2, "admin": 3}
        have = max((order.get(r, 0) for r in self.roles), default=0)
        if role == "operator":
            return have >= 1 or "operator" in self.roles or "reviewer" in self.roles
        if role == "reviewer":
            return "reviewer" in self.roles or self.is_admin
        return self.is_admin


@dataclass(frozen=True)
class OidcPending:
    verifier: str
    redirect_uri: str
    created_at: float
    nonce: str


def _checked_tenants(
    tenants: tuple[str, ...], active: str | None
) -> tuple[tuple[str, ...], str]:
    """Validate every tenant id and require ``active`` to be a member.

    ``active is None`` selects the first tenant. A present active id that is
    not in the set is rejected (it is not silently rewritten).
    """

    if not tenants:
        raise ValueError("session requires a tenant")
    checked = tuple(validate_tenant_id(str(tenant)) for tenant in tenants)
    chosen = checked[0] if active is None else validate_tenant_id(str(active))
    if chosen not in checked:
        raise ValueError("active_tenant is not a member of the session tenants")
    return checked, chosen


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class SessionStore:
    """Cookie v2 carries a session id. Roles live in sqlite (or an in-memory row)."""

    def __init__(self, secret: str | None = None, *, path: Path | None = None) -> None:
        self._hmac_key = resolve_session_secret(secret)
        self._oidc: dict[str, OidcPending] = {}
        self._oidc_lock = Lock()
        self._jti: set[str] = set()
        self._rows: dict[str, str] = {}
        self._conn = None
        if path is not None:
            from recertia.persistence.db import open_db

            self._conn = open_db(path)
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS console_sessions (
                    session_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oidc_jti (
                    jti TEXT PRIMARY KEY
                );
                """
            )
            self._conn.commit()

    def issue(self, user: ConsoleUser, *, ttl_s: int = 86400) -> str:
        tenants, active = _checked_tenants(tuple(user.tenants), user.active_tenant)
        session_id = secrets.token_urlsafe(24)
        exp = int(time.time()) + ttl_s
        payload = {
            "user_id": user.user_id,
            "display_name": user.display_name,
            "roles": sorted(user.roles),
            "tenants": list(tenants),
            "active_tenant": active,
            "csrf": secrets.token_urlsafe(24),
            "exp": exp,
        }
        self._save(session_id, payload)
        return self._token(session_id)

    def parse(self, token: str | None) -> ConsoleUser | None:
        payload = self._payload(token)
        if payload is None:
            return None
        raw_tenants = tuple(str(item) for item in (payload.get("tenants") or []))
        if not raw_tenants:
            return None
        active_raw = payload.get("active_tenant")
        try:
            tenants, active = _checked_tenants(
                raw_tenants, None if active_raw is None else str(active_raw)
            )
        except ValueError:
            return None
        roles = frozenset(str(item) for item in (payload.get("roles") or []))
        if not roles & _VALID_ROLES:
            return None
        return ConsoleUser(
            user_id=str(payload["user_id"]),
            display_name=str(payload.get("display_name") or payload["user_id"]),
            roles=roles,
            tenants=tenants,
            active_tenant=active,
        )

    def csrf_for(self, token: str | None) -> str | None:
        payload = self._payload(token)
        if payload is None:
            return None
        csrf = payload.get("csrf")
        return str(csrf) if isinstance(csrf, str) and csrf else None

    def csrf_matches(self, token: str | None, presented: str) -> bool:
        expected = self.csrf_for(token) or ""
        if not expected or len(expected) != len(presented):
            return False
        return hmac.compare_digest(expected, presented)

    def _token(self, session_id: str) -> str:
        sig = hmac.new(self._hmac_key, session_id.encode(), hashlib.sha256).hexdigest()
        return f"v2.{session_id}.{sig}"

    def _payload(self, token: str | None) -> dict[str, Any] | None:
        session_id = self._session_id(token)
        if session_id is None:
            return None
        raw = self._load(session_id)
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        if int(payload.get("exp", 0)) < int(time.time()):
            self._delete(session_id)
            return None
        return payload

    def _session_id(self, token: str | None) -> str | None:
        """v2 cookies only. A body that still carries roles does not authenticate."""

        if not token or not token.startswith("v2."):
            return None
        parts = token.split(".", 2)
        if len(parts) != 3:
            return None
        prefix, session_id, sig = parts
        if prefix != "v2" or not session_id or not sig:
            return None
        expect = hmac.new(self._hmac_key, session_id.encode(), hashlib.sha256).hexdigest()
        if len(expect) != len(sig) or not hmac.compare_digest(expect, sig):
            return None
        return session_id

    def _save(self, session_id: str, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        expires = str(payload.get("exp", ""))
        with self._oidc_lock:
            if self._conn is not None:
                self._conn.execute(
                    """INSERT INTO console_sessions(session_id, payload, expires_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(session_id) DO UPDATE SET
                         payload=excluded.payload,
                         expires_at=excluded.expires_at""",
                    (session_id, raw, expires),
                )
                self._conn.commit()
            else:
                self._rows[session_id] = raw

    def _load(self, session_id: str) -> str | None:
        with self._oidc_lock:
            if self._conn is not None:
                row = self._conn.execute(
                    "SELECT payload FROM console_sessions WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                return None if row is None else str(row[0])
            return self._rows.get(session_id)

    def _delete(self, session_id: str) -> None:
        with self._oidc_lock:
            if self._conn is not None:
                self._conn.execute(
                    "DELETE FROM console_sessions WHERE session_id=?", (session_id,)
                )
                self._conn.commit()
            else:
                self._rows.pop(session_id, None)

    def switch_tenant(self, user: ConsoleUser, tenant_id: str) -> ConsoleUser:
        tenant_id = validate_tenant_id(tenant_id)
        if tenant_id not in user.tenants:
            raise ValueError("tenant not in membership")
        return ConsoleUser(
            user_id=user.user_id,
            display_name=user.display_name,
            roles=user.roles,
            tenants=user.tenants,
            active_tenant=tenant_id,
        )

    def begin_oidc(self, *, redirect_uri: str) -> tuple[str, str, str]:
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        nonce = secrets.token_urlsafe(24)
        with self._oidc_lock:
            self._purge_oidc_locked()
            self._oidc[state] = OidcPending(verifier, redirect_uri, time.time(), nonce)
        return state, verifier, nonce

    def claim_jti(self, jti: str) -> None:
        """Remember an OIDC ``jti``. A second claim is rejected."""

        if not jti or any(ch.isspace() for ch in jti):
            raise ValueError("oidc jti missing")
        with self._oidc_lock:
            if self._conn is not None:
                try:
                    self._conn.execute("INSERT INTO oidc_jti (jti) VALUES (?)", (jti,))
                    self._conn.commit()
                except sqlite3.IntegrityError as exc:
                    raise ValueError("duplicate oidc jti") from exc
                return
            if jti in self._jti:
                raise ValueError("duplicate oidc jti")
            self._jti.add(jti)

    def take_oidc(self, state: str) -> OidcPending | None:
        with self._oidc_lock:
            self._purge_oidc_locked()
            return self._oidc.pop(state, None)

    def _purge_oidc_locked(self) -> None:
        now = time.time()
        expired = [k for k, v in self._oidc.items() if now - v.created_at > _OIDC_TTL_S]
        for key in expired:
            self._oidc.pop(key, None)


def oidc_configured() -> bool:
    return bool(
        os.environ.get("RECERTIA_OIDC_ISSUER")
        and os.environ.get("RECERTIA_OIDC_CLIENT_ID")
        and os.environ.get("RECERTIA_OIDC_CLIENT_SECRET")
    )


def oidc_authorize_url(
    *, redirect_uri: str, state: str, code_challenge: str, nonce: str
) -> str:
    issuer = os.environ["RECERTIA_OIDC_ISSUER"].rstrip("/")
    client_id = os.environ["RECERTIA_OIDC_CLIENT_ID"]
    scope = os.environ.get("RECERTIA_OIDC_SCOPE", "openid profile email")
    qs = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{issuer}/authorize?{qs}"


def _map_oidc_roles(profile: dict) -> frozenset[str]:
    claimed_raw = profile.get("roles") or profile.get("groups") or []
    if isinstance(claimed_raw, str):
        claimed = {r.strip() for r in claimed_raw.split(",") if r.strip()}
    else:
        claimed = {str(r) for r in claimed_raw}
    allow_raw = os.environ.get("RECERTIA_OIDC_ROLE_ALLOWLIST", "operator,reviewer")
    allowed = {r.strip() for r in allow_raw.split(",") if r.strip()} & _VALID_ROLES
    roles = claimed & allowed
    if not roles:
        default_raw = os.environ.get("RECERTIA_OIDC_DEFAULT_ROLES", "operator")
        roles = {r.strip() for r in default_raw.split(",") if r.strip()} & allowed
    if "admin" in roles and "admin" not in allowed:
        roles.discard("admin")
    return frozenset(roles) or frozenset({"operator"})


def _id_token_payload(token: str) -> dict[str, Any]:
    """Decode a JWT payload. Signature verification needs a JWKS client we do not ship."""

    parts = token.split(".")
    if len(parts) != 3 or not parts[1]:
        raise HTTPException(status_code=400, detail="oidc id_token malformed")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="oidc id_token malformed") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="oidc id_token malformed")
    return payload


def _check_id_token(token: str, *, nonce: str, sessions: SessionStore) -> None:
    payload = _id_token_payload(token)
    got = payload.get("nonce")
    if (
        not isinstance(got, str)
        or len(got) != len(nonce)
        or not hmac.compare_digest(got, nonce)
    ):
        raise HTTPException(status_code=400, detail="oidc nonce rejected")
    jti = payload.get("jti")
    if not isinstance(jti, str):
        raise HTTPException(status_code=400, detail="oidc jti missing")
    try:
        sessions.claim_jti(jti)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def oidc_exchange_code(
    *,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    nonce: str,
    sessions: SessionStore,
) -> ConsoleUser:
    """Exchange auth code for tokens and map to ConsoleUser (generic OIDC + PKCE)."""

    issuer = os.environ["RECERTIA_OIDC_ISSUER"].rstrip("/")
    token_url = os.environ.get("RECERTIA_OIDC_TOKEN_URL", f"{issuer}/token")
    userinfo_url = os.environ.get("RECERTIA_OIDC_USERINFO_URL", f"{issuer}/userinfo")
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": os.environ["RECERTIA_OIDC_CLIENT_ID"],
        "client_secret": os.environ["RECERTIA_OIDC_CLIENT_SECRET"],
        "code_verifier": code_verifier,
    }
    try:
        tok_req = urllib.request.Request(
            token_url,
            data=urllib.parse.urlencode(data).encode(),
            headers={"content-type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(tok_req, timeout=30) as resp:
            tok_payload = json.loads(resp.read().decode())
        access = tok_payload.get("access_token")
        if not access:
            raise HTTPException(status_code=502, detail="oidc token missing access_token")
        id_token = tok_payload.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise HTTPException(status_code=502, detail="oidc token missing id_token")
        _check_id_token(id_token, nonce=nonce, sessions=sessions)
        info_req = urllib.request.Request(
            userinfo_url,
            headers={"authorization": f"Bearer {access}"},
            method="GET",
        )
        with urllib.request.urlopen(info_req, timeout=30) as resp:
            profile = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"oidc exchange failed: {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=502, detail=f"oidc network error: {exc}") from exc
    user_id = str(profile.get("sub") or profile.get("email") or "")
    if not user_id:
        raise HTTPException(status_code=502, detail="oidc userinfo missing sub")
    roles = _map_oidc_roles(profile)
    tenants_raw = profile.get("tenants") or os.environ.get(
        "RECERTIA_OIDC_DEFAULT_TENANTS", "default"
    )
    if isinstance(tenants_raw, str):
        claimed = tuple(t.strip() for t in tenants_raw.split(",") if t.strip())
    else:
        claimed = tuple(str(t) for t in tenants_raw)
    try:
        tenants, active = _checked_tenants(claimed or ("default",), None)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="oidc tenant id rejected") from exc
    return ConsoleUser(
        user_id=user_id,
        display_name=str(profile.get("name") or profile.get("email") or user_id),
        roles=roles,
        tenants=tenants,
        active_tenant=active,
    )


def console_session(sessions: SessionStore, request: Request) -> ConsoleUser | None:
    """Parse a browser session. Auth mode ``off`` ignores cookies and headers."""

    if auth_mode() == "off":
        return None
    token = request.headers.get("X-Recertia-Session") or request.cookies.get("recertia_session")
    return sessions.parse(token)


def require_console_user(
    request: Request,
    sessions: SessionStore,
    *,
    min_role: Role = "operator",
    x_recertia_session: str | None = Header(default=None, alias="X-Recertia-Session"),
    x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
) -> ConsoleUser:
    del x_recertia_session
    user = console_session(sessions, request)
    if user is None:
        raise HTTPException(status_code=401, detail="console authentication required")
    if not user.may(min_role):
        raise HTTPException(status_code=403, detail=f"requires role {min_role}")
    if x_recertia_tenant:
        if x_recertia_tenant not in user.tenants:
            raise HTTPException(status_code=403, detail="tenant not in membership")
        user = sessions.switch_tenant(user, x_recertia_tenant)
    return user
