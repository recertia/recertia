"""SEC-7: tool grants come from the PDP, and T2 decisions share one function."""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.policy import IsolationSettings, Policy
from recertia.app.authz import PolicyDecisionPoint, Resource
from recertia.bootstrap import build_default_orchestrator
from recertia.errors import ExecutionUnavailable, Forbidden, NotFound
from recertia.ports.identity import Principal
from recertia.settings import Settings


def _policy(**kwargs: object) -> Policy:
    return Policy(version="sec7", authoring_prior_version="sec7", **kwargs)  # type: ignore[arg-type]


def _runs_only() -> Principal:
    return Principal.from_api_key(
        key_id="runs", tenant_id="t1", scopes=frozenset({"runs"})
    )


def test_container_grant_does_not_include_external_computer() -> None:
    policy = _policy(isolation=IsolationSettings(default_backend="local"))
    pdp = PolicyDecisionPoint(policy, "container")
    pdp.approve_tools(_runs_only(), origin="tool")
    assert pdp.is_approved("shell", "step")
    assert pdp.is_approved("edit_file", "step")
    assert pdp.is_approved("agent_subtask", "step")
    assert pdp.is_approved("fetch", "step")
    assert pdp.is_approved("read_file", "step")
    assert not pdp.is_approved("external_computer", "step")
    assert pdp.backend == "container"


def test_local_does_not_blanket_approve_and_exec_is_named() -> None:
    policy = _policy(isolation=IsolationSettings(default_backend="container"))
    runs = _runs_only()
    denied = PolicyDecisionPoint(policy, "local")
    denied.approve_tools(runs, origin="tool")
    assert denied.is_approved("grep", "step")
    assert not denied.is_approved("shell", "step")
    assert not denied.is_approved("fetch", "step")
    assert not denied.is_approved("external_computer", "step")

    exec_principal = Principal.from_api_key(
        key_id="exec", tenant_id="t1", scopes=frozenset({"runs", "exec"})
    )
    allowed = PolicyDecisionPoint(policy, "local")
    allowed.approve_tools(exec_principal, origin="tool")
    assert allowed.is_approved("shell", "step")
    assert allowed.is_approved("edit_file", "step")
    assert not allowed.is_approved("fetch", "step")
    assert not allowed.is_approved("external_computer", "step")


def test_model_origin_on_local_gets_no_write_grant() -> None:
    policy = _policy()
    principal = Principal.local_operator()
    pdp = PolicyDecisionPoint(policy, "local")
    pdp.approve_tools(principal, origin="model")
    assert pdp.is_approved("read_file", "step")
    assert not pdp.is_approved("shell", "step")


def test_policy_flag_false_grants_writes_and_multi_node_refuses_it() -> None:
    policy = _policy(require_tool_approval_for_non_read=False)
    pdp = PolicyDecisionPoint(policy, "local", deployment_mode="single_node")
    pdp.approve_tools(_runs_only(), origin="tool")
    assert pdp.is_approved("shell", "step")
    assert pdp.require_tool_approval_for_non_read is False
    with pytest.raises(ExecutionUnavailable, match="require_tool_approval_for_non_read"):
        PolicyDecisionPoint(policy, "local", deployment_mode="multi_node")


def test_pdp_uses_resolved_backend_only() -> None:
    source = Path("src/recertia/app/authz.py").read_text(encoding="utf-8")
    assert "default_backend" not in source
    policy = _policy(isolation=IsolationSettings(default_backend="container"))
    settings = Settings(
        execution_backend="local",
        execution_backend_was_set=True,
        deployment_mode="single_node",
    )
    opened = PolicyDecisionPoint.open(settings, policy, surface="api")
    opened.approve_tools(_runs_only(), origin="tool")
    assert opened.backend == "local"
    assert not opened.is_approved("shell", "step")


def test_may_decide_proposal_shares_the_t2_rule() -> None:
    pdp = PolicyDecisionPoint(_policy(), "local")
    runs = _runs_only()
    admin = Principal.from_api_key(
        key_id="admin", tenant_id="t1", scopes=frozenset({"runs", "admin"})
    )
    assert pdp.may_decide_proposal(runs, kind="skill", payload={"tier": "T1"})
    assert not pdp.may_decide_proposal(
        runs, kind="policy", payload={"tier": "T2"}, console_may_review=None
    )
    assert pdp.may_decide_proposal(
        admin, kind="correction", payload={}, console_may_review=None
    )
    assert pdp.may_decide_proposal(
        runs, kind="policy", payload={"tier": "T2"}, console_may_review=True
    )
    assert not pdp.may_decide_proposal(
        admin, kind="policy", payload={"tier": "T2"}, console_may_review=False
    )


def test_bootstrap_wires_approval_flag_and_skips_local_blanket(tmp_path: Path) -> None:
    bundle = build_default_orchestrator(
        tmp_path / "runs",
        skills_root=tmp_path / "skills",
        facts_root=tmp_path / "facts",
    )
    try:
        tools = bundle.orchestrator.tools
        assert tools is not None
        assert tools.require_approval_for_non_read is True
        gate = tools.approval_gate
        assert gate is not None
        assert gate.is_approved("shell", "step")
        assert not gate.is_approved("external_computer", "step")
        assert not gate.is_approved("fetch", "step")
    finally:
        bundle.close()

    runs = _runs_only()
    limited = build_default_orchestrator(
        tmp_path / "runs-api",
        skills_root=tmp_path / "skills",
        facts_root=tmp_path / "facts",
        principal=runs,
        surface="api",
        approve_default_tools=False,
    )
    try:
        gate = limited.orchestrator.tools.approval_gate
        assert gate is not None
        assert gate.is_approved("read_file", "step")
        assert not gate.is_approved("shell", "step")
    finally:
        limited.close()


def test_membership_role_beats_scope_and_cli_stays_platform_admin() -> None:
    pdp = PolicyDecisionPoint(_policy(), "local")
    operator = Principal.local_operator()
    assert pdp.tenant_access(operator, "operator") == "platform_admin"
    admin_key = Principal.from_api_key(key_id="k", tenant_id="t1", scopes=frozenset({"admin"}))
    assert pdp.tenant_access(admin_key, "operator") == "operator"
    assert pdp.tenant_access(admin_key, "tenant_admin") == "tenant_admin"
    assert pdp.tenant_access(admin_key, None) == "tenant_admin"
    runs = _runs_only()
    assert pdp.tenant_access(runs, None) == "operator"
    assert pdp.may_admin_registry(runs, membership_role="tenant_admin")
    assert not pdp.may_admin_registry(admin_key, membership_role="operator")


def _pdp() -> PolicyDecisionPoint:
    return PolicyDecisionPoint(_policy(), "local")


def test_runs_create_requires_runs_scope() -> None:
    pdp = _pdp()
    resource = Resource(tenant_id="t1")
    pdp.require(_runs_only(), "runs.create", resource)
    metrics = Principal.from_api_key(
        key_id="metrics", tenant_id="t1", scopes=frozenset({"metrics"})
    )
    with pytest.raises(Forbidden):
        pdp.require(metrics, "runs.create", resource)


def test_runs_script_requires_exec_or_admin() -> None:
    pdp = _pdp()
    resource = Resource(tenant_id="t1")
    with pytest.raises(Forbidden):
        pdp.require(_runs_only(), "runs.script", resource)
    exec_principal = Principal.from_api_key(
        key_id="exec", tenant_id="t1", scopes=frozenset({"runs", "exec"})
    )
    pdp.require(exec_principal, "runs.script", resource)
    admin = Principal.from_api_key(
        key_id="admin", tenant_id="t1", scopes=frozenset({"runs", "admin"})
    )
    pdp.require(admin, "runs.script", resource)


def test_runs_cancel_other_tenant_is_not_found() -> None:
    pdp = _pdp()
    principal = _runs_only()
    pdp.require(principal, "runs.cancel", Resource(tenant_id="t1"))
    with pytest.raises(NotFound):
        pdp.require(principal, "runs.cancel", Resource(tenant_id="t2"))


def test_workspace_register_requires_platform_admin() -> None:
    pdp = _pdp()
    tenant_admin = Principal.from_api_key(
        key_id="admin", tenant_id="t1", scopes=frozenset({"runs", "admin"})
    )
    with pytest.raises(Forbidden):
        pdp.require(tenant_admin, "workspace.register")
    host = Principal.from_api_key(
        key_id="host", tenant_id="t1", scopes=frozenset({"runs", "platform_admin"})
    )
    pdp.require(host, "workspace.register")
    pdp.require(Principal.local_operator(), "workspace.register")


def test_skill_promote_seed_denied_for_tenant_admin() -> None:
    pdp = _pdp()
    tenant_admin = Principal.from_api_key(
        key_id="admin", tenant_id="t1", scopes=frozenset({"runs", "admin"})
    )
    with pytest.raises(Forbidden):
        pdp.require(tenant_admin, "skill.promote_seed")
