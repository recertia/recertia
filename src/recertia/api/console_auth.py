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
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from threading import Lock
from typing import Literal, TypedDict
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
        "samesite": "lax",
        "secure": cookie_secure(),
        "path": "/",
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
    """Signed cookie sessions (HMAC) — no server DB required for C3."""

    def __init__(self, secret: str | None = None) -> None:
        self._hmac_key = resolve_session_secret(secret)
        self._oidc: dict[str, OidcPending] = {}
        self._oidc_lock = Lock()

    def issue(self, user: ConsoleUser, *, ttl_s: int = 86400) -> str:
        tenants, active = _checked_tenants(tuple(user.tenants), user.active_tenant)
        payload = {
            "user_id": user.user_id,
            "display_name": user.display_name,
            "roles": sorted(user.roles),
            "tenants": list(tenants),
            "active_tenant": active,
            "exp": int(time.time()) + ttl_s,
        }
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        sig = hmac.new(self._hmac_key, body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{sig}"

    def parse(self, token: str | None) -> ConsoleUser | None:
        if not token or "." not in token:
            return None
        body, _, sig = token.rpartition(".")
        expect = hmac.new(self._hmac_key, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, sig):
            return None
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return None
        if int(payload.get("exp", 0)) < int(time.time()):
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
        roles = frozenset(payload.get("roles") or [])
        if not roles & _VALID_ROLES:
            return None
        return ConsoleUser(
            user_id=str(payload["user_id"]),
            display_name=str(payload.get("display_name") or payload["user_id"]),
            roles=roles,
            tenants=tenants,
            active_tenant=active,
        )

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

    def begin_oidc(self, *, redirect_uri: str) -> tuple[str, str]:
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        with self._oidc_lock:
            self._purge_oidc_locked()
            self._oidc[state] = OidcPending(verifier, redirect_uri, time.time())
        return state, verifier

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
    *, redirect_uri: str, state: str, code_challenge: str
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


def oidc_exchange_code(
    *, code: str, redirect_uri: str, code_verifier: str
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


def require_console_user(
    request: Request,
    sessions: SessionStore,
    *,
    min_role: Role = "operator",
    x_recertia_session: str | None = Header(default=None, alias="X-Recertia-Session"),
    x_recertia_tenant: str | None = Header(default=None, alias="X-Recertia-Tenant"),
) -> ConsoleUser:
    token = x_recertia_session or request.cookies.get("recertia_session")
    user = sessions.parse(token)
    if user is None:
        raise HTTPException(status_code=401, detail="console authentication required")
    if not user.may(min_role):
        raise HTTPException(status_code=403, detail=f"requires role {min_role}")
    if x_recertia_tenant:
        if x_recertia_tenant not in user.tenants:
            raise HTTPException(status_code=403, detail="tenant not in membership")
        user = sessions.switch_tenant(user, x_recertia_tenant)
    return user
