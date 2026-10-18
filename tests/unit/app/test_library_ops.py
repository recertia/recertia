"""CLI and API library, program, workspace, key, and ops paths go through services."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from contracts.policy import Policy
from recertia.app.authz import PolicyDecisionPoint, Resource
from recertia.app.keys import KeyService
from recertia.app.library import LibraryService
from recertia.app.programs import ProgramService
from recertia.app.workspaces import WorkspaceService
from recertia.errors import Forbidden, NotFound
from recertia.ports.identity import Principal

ROOT = Path(__file__).resolve().parents[3]


def test_cli_and_api_call_library_ops_services() -> None:
    cli_skills = (ROOT / "src/recertia/cli/skills.py").read_text(encoding="utf-8")
    cli_keys = (ROOT / "src/recertia/cli/keys.py").read_text(encoding="utf-8")
    cli_ws = (ROOT / "src/recertia/cli/workspaces.py").read_text(encoding="utf-8")
    cli_policy = (ROOT / "src/recertia/cli/policy_cmd.py").read_text(encoding="utf-8")
    cli_backup = (ROOT / "src/recertia/cli/backup_cmd.py").read_text(encoding="utf-8")
    cli_runs = (ROOT / "src/recertia/cli/runs.py").read_text(encoding="utf-8")
    api_lib = (ROOT / "src/recertia/api/routes/library.py").read_text(encoding="utf-8")
    api_skills = (ROOT / "src/recertia/api/routes/skills.py").read_text(encoding="utf-8")
    api_prog = (ROOT / "src/recertia/api/routes/programs.py").read_text(encoding="utf-8")
    api_ws = (ROOT / "src/recertia/api/routes/workspaces.py").read_text(encoding="utf-8")
    assert "LibraryService" in cli_skills
    assert "lint_store(" not in cli_skills
    assert "KeyService" in cli_keys
    assert "WorkspaceService" in cli_ws
    assert "LibraryService" in cli_policy
    assert "OpsService" in cli_backup
    assert "CheckpointReader" in cli_runs
    assert "library_service." in api_lib
    assert "library_service." in api_skills
    assert "program_service." in api_prog
    assert "workspace_service." in api_ws
    assert "ctx.programs.put" not in api_prog
    assert "ctx.workspace_registry.register" not in api_ws


def test_library_read_requires_runs_scope() -> None:
    class _Pdp:
        def require(self, principal: Principal, action: str, resource: Any) -> None:
            del resource
            if action != "skills.read" or not principal.has_scope("runs"):
                raise Forbidden("missing scope: runs")

    service = LibraryService(pdp=_Pdp())
    metrics = Principal.from_api_key(
        key_id="k", tenant_id="t1", scopes=frozenset({"metrics"})
    )
    with pytest.raises(Forbidden, match="runs"):
        service.list_templates(metrics)


def test_keys_issue_requires_admin() -> None:
    class _Pdp:
        def require(self, principal: Principal, action: str, resource: Any) -> None:
            del resource
            if action != "keys.issue" or not principal.has_scope("admin"):
                raise Forbidden("missing scope: admin")

    service = KeyService(pdp=_Pdp())
    runs = Principal.from_api_key(
        key_id="k", tenant_id="t1", scopes=frozenset({"runs"})
    )
    with pytest.raises(Forbidden, match="admin"):
        service.issue(runs, store=object(), tenant_id="t1", scopes={"runs"}, actor="t")


def test_program_get_missing_is_not_found() -> None:
    class _Store:
        def get(self, program_id: str, tenant_id: str) -> None:
            del program_id, tenant_id
            return None

    service = ProgramService(pdp=None)
    runtime = type("R", (), {"programs": _Store()})()
    with pytest.raises(NotFound, match="program not found"):
        service.get(
            Principal.local_operator(), tenant="t1", runtime=runtime, program_id="missing"
        )


def test_pdp_library_actions_allow_runs_scope() -> None:
    pdp = PolicyDecisionPoint(
        Policy(version="t", authoring_prior_version="t"),  # type: ignore[arg-type]
        "local",
    )
    runs = Principal.from_api_key(
        key_id="runs", tenant_id="t1", scopes=frozenset({"runs"})
    )
    resource = Resource(tenant_id="t1")
    pdp.require(runs, "skills.read", resource)
    pdp.require(runs, "programs.write", resource)
    pdp.require(runs, "ledger.verify", resource)
    pdp.require(runs, "workspaces.read", resource)
    blobs = Principal.from_api_key(
        key_id="blobs", tenant_id="t1", scopes=frozenset({"blobs"})
    )
    pdp.require(blobs, "blobs.write", resource)
    with pytest.raises(Forbidden):
        pdp.require(runs, "blobs.write", resource)
    with pytest.raises(Forbidden):
        pdp.require(runs, "keys.issue", resource)
    pdp.require(Principal.local_operator(), "ops.backup")
    pdp.require(Principal.local_operator(), "tenants.switch")


def test_workspace_write_requires_admin() -> None:
    class _Pdp:
        def require(self, principal: Principal, action: str, resource: Any) -> None:
            del resource
            if action != "workspaces.write":
                raise Forbidden("missing scope: admin")
            if not (principal.has_scope("admin") or principal.has_role("admin")):
                raise Forbidden("missing scope: admin")

        def may_admin_registry(self, principal: Principal, **_kwargs: Any) -> bool:
            return principal.has_scope("admin") or principal.has_role("admin")

    service = WorkspaceService(pdp=_Pdp())
    runs = Principal.from_api_key(
        key_id="k", tenant_id="t1", scopes=frozenset({"runs"})
    )
    with pytest.raises(Forbidden, match="admin"):
        service.disable(
            runs, registry=object(), tenant_id="t1", workspace_id="ws"
        )
