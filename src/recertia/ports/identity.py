"""Authenticated identity. The only definition of ``TenantId`` and ``Principal``."""

from __future__ import annotations

import getpass
import platform
import re
from dataclasses import dataclass
from typing import Literal

from recertia.errors import InvalidTenantId

_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# Wire scopes an API key may carry. ``platform_admin`` is also a role: tenant
# ``admin`` does not imply it. SEC-8 skips a distinct ``evals`` key scope so
# existing metrics keys keep calling eval routes. Reissue is a later break.
OPERATOR_SCOPES = frozenset({"admin", "runs", "blobs", "metrics", "exec", "promote", "jobs"})
_TENANT_LOCAL_SCOPES = OPERATOR_SCOPES - {"admin"}

PrincipalKind = Literal["api_key", "console_session", "local_operator", "executor", "system"]


class TenantId(str):
    """Validated tenant id. Constructing it runs the tenant pattern."""

    def __new__(cls, value: str) -> TenantId:
        if not isinstance(value, str) or _TENANT_RE.fullmatch(value) is None:
            raise InvalidTenantId(
                "tenant_id must match ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ "
                "(no path separators or traversal)"
            )
        return str.__new__(cls, value)


def tenant_id(value: str) -> TenantId:
    """Accept ``^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$``. Raise ``InvalidTenantId`` otherwise."""

    return TenantId(value)


@dataclass(frozen=True)
class Principal:
    subject: str
    kind: PrincipalKind
    tenant: TenantId
    tenants: tuple[TenantId, ...]
    scopes: frozenset[str]
    roles: frozenset[str]

    @property
    def key_id(self) -> str:
        """Compatibility: api-key id, or subject for session and CLI. One release."""

        return self.subject

    @property
    def tenant_id(self) -> str:
        """Compatibility: ``str(self.tenant)``. One release."""

        return str(self.tenant)

    def has_scope(self, scope: str) -> bool:
        if scope in self.scopes or "platform_admin" in self.scopes:
            return True
        # Tenant admin satisfies tenant-local scopes. It does not imply platform_admin.
        if scope != "platform_admin" and "admin" in self.scopes:
            return True
        return False

    def has_role(self, role: str) -> bool:
        if role in self.roles or "platform_admin" in self.roles:
            return True
        if role in {"operator", "reviewer", "admin"} and "admin" in self.roles:
            return True
        return False

    @classmethod
    def local_operator(
        cls, *, tenant: TenantId | None = None, actor: str | None = None
    ) -> Principal:
        """D5. The CLI operator: ``platform_admin`` plus the operational scopes."""

        active = tenant if tenant is not None else TenantId("default")
        subject = actor or f"cli:{getpass.getuser()}@{platform.node() or 'localhost'}"
        return cls(
            subject=subject,
            kind="local_operator",
            tenant=active,
            tenants=(active,),
            scopes=OPERATOR_SCOPES,
            roles=frozenset({"platform_admin"}),
        )

    @classmethod
    def from_api_key(cls, *, key_id: str, tenant_id: str, scopes: frozenset[str]) -> Principal:
        tenant = TenantId(tenant_id)
        roles: set[str] = set()
        if "admin" in scopes:
            roles.add("admin")
        if "platform_admin" in scopes:
            roles.add("platform_admin")
        return cls(
            subject=key_id,
            kind="api_key",
            tenant=tenant,
            tenants=(tenant,),
            scopes=frozenset(scopes),
            roles=frozenset(roles),
        )
