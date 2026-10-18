"""Policy decision point: action require/decide, tool grants, and proposal decisions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from contracts.policy import Policy
from recertia.errors import ExecutionUnavailable, Forbidden, NotFound
from recertia.ports.execution import CommandOrigin, ExecutionBackend, Surface
from recertia.ports.identity import Principal
from recertia.settings import Settings

# Named tools. A grant is never "every name currently in the registry".
_READ_TOOLS = ("read_file", "grep")
_CONTAINER_WRITES = ("shell", "edit_file", "agent_subtask", "fetch")
_LOCAL_EXEC_WRITES = ("shell", "edit_file", "agent_subtask")


@dataclass(frozen=True)
class Resource:
    tenant_id: str | None = None
    run_id: str | None = None
    kind: str | None = None  # e.g. "_platform" for secrets


@dataclass(frozen=True)
class Decision:
    allow: bool
    status: int = 200
    reason: str = ""


@dataclass
class ApprovalRecord:
    tool: str
    step_id: str
    actor: str
    reason: str = ""


@dataclass
class ToolGrants:
    """Allowlist of approved (tool, step) or tool-wide grants for one run."""

    _grants: set[tuple[str, str | None]] = field(default_factory=set)
    _records: list[ApprovalRecord] = field(default_factory=list)

    def approve(
        self,
        tool: str,
        *,
        step_id: str | None = None,
        actor: str = "pdp",
        reason: str = "",
    ) -> None:
        self._grants.add((tool, step_id))
        self._records.append(
            ApprovalRecord(tool=tool, step_id=step_id or "*", actor=actor, reason=reason)
        )

    def is_approved(self, tool: str, step_id: str) -> bool:
        return (tool, step_id) in self._grants or (tool, None) in self._grants

    def clear(self) -> None:
        self._grants.clear()
        self._records.clear()


class PolicyDecisionPoint:
    """Authorizer and approval policy. Holds a resolved backend, not the policy field.

    Routes and services call ``require`` / ``decide`` for the SEC-7 action table.
    """

    def __init__(
        self,
        policy: Policy,
        backend: ExecutionBackend,
        *,
        deployment_mode: str = "single_node",
    ) -> None:
        if deployment_mode == "multi_node" and not policy.require_tool_approval_for_non_read:
            raise ExecutionUnavailable(
                "multi_node refuses require_tool_approval_for_non_read=false"
            )
        self._policy = policy
        self._backend = backend
        self.grants = ToolGrants()

    @property
    def backend(self) -> ExecutionBackend:
        return self._backend

    @property
    def require_tool_approval_for_non_read(self) -> bool:
        return self._policy.require_tool_approval_for_non_read

    @classmethod
    def open(cls, settings: Settings, policy: Policy, *, surface: Surface) -> PolicyDecisionPoint:
        """Resolve the backend once, then build the decision point."""

        from recertia.solver.executor import resolve_backend

        backend = resolve_backend(settings, policy, surface=surface, capability=None)
        return cls(policy, backend, deployment_mode=settings.deployment_mode)

    @classmethod
    def load(cls, *, surface: Surface = "api") -> PolicyDecisionPoint:
        from recertia.policy_load import load_policy
        from recertia.settings import load_settings

        settings = load_settings()
        return cls.open(settings, load_policy(), surface=surface)

    def approve_tools(
        self,
        principal: Principal | None,
        *,
        origin: CommandOrigin = "tool",
        backend: ExecutionBackend | None = None,
    ) -> ToolGrants:
        """Fill this run's grant set and return it.

        Container runs receive the sandboxed write tools. Local runs do not:
        a principal with ``exec`` or ``admin`` may receive shell, edit, and
        agent_subtask, and nothing else. ``external_computer`` is granted only
        when the isolation allowlist already allows it.
        """

        resolved = backend if backend is not None else self._backend
        self.grants.clear()
        for name in _READ_TOOLS:
            self.grants.approve(name, actor="pdp", reason="read")
        if origin == "model" and resolved == "local":
            return self.grants
        if not self._policy.require_tool_approval_for_non_read:
            for name in _CONTAINER_WRITES:
                self.grants.approve(name, actor="pdp", reason="approval-disabled")
        elif resolved == "container":
            for name in _CONTAINER_WRITES:
                self.grants.approve(name, actor="pdp", reason="container-default")
        elif _principal_may_exec(principal):
            for name in _LOCAL_EXEC_WRITES:
                self.grants.approve(name, actor="pdp", reason="exec-scope")
        if _external_computer_allowed(self._policy):
            self.grants.approve(
                "external_computer", actor="pdp", reason="external-computer-allowlist"
            )
        return self.grants

    def is_approved(self, tool: str, step_id: str) -> bool:
        return self.grants.is_approved(tool, step_id)

    def may_decide_proposal(
        self,
        principal: Principal,
        *,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        console_may_review: bool | None = None,
    ) -> bool:
        """Whether this caller may record a decision.

        Non-T2 proposals are allowed for any authenticated runs caller. A
        correction or ``tier == T2`` proposal needs a reviewer session, or an
        API key that carries ``admin``.
        """

        body = payload or {}
        tier = body.get("tier") if isinstance(body, Mapping) else None
        if kind != "correction" and tier != "T2":
            return True
        if console_may_review is not None:
            return bool(console_may_review)
        return "admin" in principal.scopes

    def may_admin_host(
        self, principal: Principal, *, console_roles: frozenset[str] | None = None
    ) -> bool:
        """Registering a host root is platform_admin. Tenant admin is not enough."""

        if console_roles is not None:
            return "platform_admin" in console_roles
        return principal.has_role("platform_admin") or "platform_admin" in principal.scopes

    def tenant_access(self, principal: Principal, recorded: str | None = None) -> str:
        """``platform_admin`` for the CLI operator. A membership row is tenant_admin or operator."""

        if principal.kind == "local_operator" or principal.has_role("platform_admin"):
            return "platform_admin"
        if recorded in {"tenant_admin", "admin"}:
            return "tenant_admin"
        if recorded == "operator":
            return "operator"
        if "admin" in principal.scopes or principal.has_role("admin"):
            return "tenant_admin"
        return "operator"

    def may_admin_registry(
        self,
        principal: Principal,
        *,
        console_is_admin: bool | None = None,
        membership_role: str | None = None,
    ) -> bool:
        """Patching or disabling a workspace is tenant admin or platform_admin."""

        if console_is_admin is not None:
            return console_is_admin
        return self.tenant_access(principal, membership_role) in {"platform_admin", "tenant_admin"}

    def decide(
        self,
        principal: Principal,
        action: str,
        resource: Resource | None = None,
    ) -> Decision:
        """Allow or deny ``action`` without raising. Unknown actions are denied."""

        res = resource if resource is not None else Resource()
        if action in {"runs.create", "runs.read"}:
            if not principal.has_scope("runs"):
                return Decision(allow=False, status=403, reason="missing scope: runs")
            if not _tenant_matches(principal, res):
                return Decision(allow=False, status=403, reason="tenant mismatch")
            return Decision(allow=True)
        if action == "runs.script":
            if not _principal_may_exec(principal):
                return Decision(
                    allow=False,
                    status=403,
                    reason="script requires the 'exec' scope (or admin)",
                )
            return Decision(allow=True)
        if action == "runs.cancel":
            if not principal.has_scope("runs"):
                return Decision(allow=False, status=403, reason="missing scope: runs")
            if (
                res.tenant_id is not None
                and res.tenant_id != principal.tenant_id
                and not _platform_admin(principal)
            ):
                return Decision(allow=False, status=404, reason="not found")
            return Decision(allow=True)
        if action == "runs.resume":
            if not principal.has_scope("runs"):
                return Decision(allow=False, status=403, reason="missing scope: runs")
            return Decision(allow=True)
        if action in {"skill.promote", "skills.promote"}:
            if principal.has_scope("promote") or principal.has_role("reviewer"):
                return Decision(allow=True)
            return Decision(allow=False, status=403, reason="missing scope: promote")
        if action == "skill.promote_seed":
            if not _platform_admin(principal):
                return Decision(
                    allow=False, status=403, reason="missing scope: platform_admin"
                )
            return Decision(allow=True)
        if action in {"job.run", "jobs.run"}:
            if not principal.has_scope("jobs"):
                return Decision(allow=False, status=403, reason="missing scope: jobs")
            return Decision(allow=True)
        if action in {"eval.run", "evals.run", "metrics.read"}:
            # SEC-8 currently skips a distinct evals key scope so existing
            # metrics keys keep calling eval routes. Treat metrics OR evals.
            if principal.has_scope("evals") or principal.has_scope("metrics"):
                return Decision(allow=True)
            return Decision(allow=False, status=403, reason="missing scope: evals")
        if action == "policy.propose":
            if not principal.has_scope("runs"):
                return Decision(allow=False, status=403, reason="missing scope: runs")
            return Decision(allow=True)
        if action == "policy.apply":
            if not (principal.has_scope("admin") or principal.has_role("admin")):
                return Decision(allow=False, status=403, reason="missing scope: admin")
            if not _tenant_matches(principal, res):
                return Decision(allow=False, status=403, reason="tenant mismatch")
            return Decision(allow=True)
        if action == "workspace.register":
            if not self.may_admin_host(principal):
                return Decision(
                    allow=False, status=403, reason="missing scope: platform_admin"
                )
            return Decision(allow=True)
        if action == "membership.grant":
            if not _platform_admin(principal):
                return Decision(
                    allow=False, status=403, reason="missing scope: platform_admin"
                )
            return Decision(allow=True)
        if action == "secret.write":
            if res.kind == "_platform" or res.tenant_id == "_platform":
                if not _platform_admin(principal):
                    return Decision(
                        allow=False, status=403, reason="missing scope: platform_admin"
                    )
                return Decision(allow=True)
            if not (principal.has_scope("admin") or principal.has_role("admin")):
                return Decision(allow=False, status=403, reason="missing scope: admin")
            if not _tenant_matches(principal, res):
                return Decision(allow=False, status=403, reason="tenant mismatch")
            return Decision(allow=True)
        return Decision(allow=False, status=403, reason=f"unknown action: {action}")

    def require(
        self,
        principal: Principal,
        action: str,
        resource: Resource | None = None,
    ) -> None:
        """Raise ``Forbidden`` (403) or ``NotFound`` (404) when ``decide`` denies."""

        decision = self.decide(principal, action, resource)
        if decision.allow:
            return
        if decision.status == 404:
            raise NotFound(decision.reason or "not found")
        raise Forbidden(decision.reason or "forbidden")


def _platform_admin(principal: Principal) -> bool:
    return principal.has_role("platform_admin") or "platform_admin" in principal.scopes


def _tenant_matches(principal: Principal, resource: Resource) -> bool:
    if resource.tenant_id is None:
        return True
    return resource.tenant_id == principal.tenant_id or _platform_admin(principal)


def _principal_may_exec(principal: Principal | None) -> bool:
    if principal is None:
        return False
    return "exec" in principal.scopes or "admin" in principal.scopes


def assert_run_executable(
    principal: Principal,
    *,
    script: bool,
    settings: Settings | None = None,
) -> None:
    """SEC-2 / D2: raw ``script`` needs ``exec`` or ``admin``; commands stay on ``runs``.

    ``settings`` is accepted so routes and services can share one call site.
    Multi-node refusal of the local backend is owned by
    ``Settings.validate_deployment``; this gate does not re-check that pair.
    """

    del settings  # reserved; validate_deployment owns multi_node + local
    if script and not principal.has_scope("exec"):
        raise Forbidden("script requires the 'exec' scope (or admin)")


def _external_computer_allowed(policy: Policy) -> bool:
    isolation = policy.isolation
    return bool(
        isolation.allow_external_computer
        and isolation.long_lived_computer_backend
        and isolation.external_computer_allowlist
    )

