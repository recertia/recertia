"""Quota admission has one implementation, and routes call that service."""

from __future__ import annotations

import ast
from pathlib import Path

from recertia.app.runs import RunService

API = Path(__file__).resolve().parents[3] / "src" / "recertia" / "api"
RUN_SERVICE = API.parent / "app" / "runs.py"


def test_routes_do_not_call_quota_admit() -> None:
    hits = [
        path.name
        for path in API.rglob("*.py")
        if "quota_store.admit" in path.read_text(encoding="utf-8")
        or "quota.admit" in path.read_text(encoding="utf-8")
    ]
    assert hits == []


def test_routes_delegate_create_and_resume() -> None:
    text = (API / "routes" / "runs.py").read_text(encoding="utf-8")
    assert "run_service.create(" in text
    assert "run_service.resume(" in text
    assert "orchestrator.start" not in text
    assert "orchestrator.resume" not in text


def test_service_admit_is_the_quota_call() -> None:
    seen: list[str] = []

    class _Quota:
        def admit(self, tenant_id: str, *, quota: object = None) -> None:
            del quota
            seen.append(tenant_id)

        def release_inflight(self, tenant_id: str) -> None:
            seen.append(f"release:{tenant_id}")

        def complete(self, tenant_id: str, *, cost_usd: float = 0.0) -> None:
            seen.append(f"complete:{tenant_id}:{cost_usd}")

    runs: dict[tuple[str, str], object] = {}
    service = RunService(_Quota(), runs, lambda tenant, run_id: None)
    service.admit("t1")
    service.complete("t1", cost_usd=1.5)
    service.release("t1")
    assert seen == ["t1", "complete:t1:1.5", "release:t1"]
    assert service.get("t1", "missing") is None


def test_service_does_not_drive_orchestrator() -> None:
    """RunService admits and records runs; a worker or the CLI drives the orchestrator.

    Checked on the syntax tree, so an aliased receiver or a call split over lines is
    still caught.
    """

    tree = ast.parse(RUN_SERVICE.read_text(encoding="utf-8"), filename=str(RUN_SERVICE))
    drives: list[str] = []
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            receiver = ast.unparse(node.func.value).lower()
            if node.func.attr in {"start", "resume"} and "orchestrat" in receiver:
                drives.append(f"{ast.unparse(node.func)} (line {node.lineno})")
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names = [alias.name for alias in node.names]
            if node.module.startswith("recertia.graph.engine") or any("Orchestrator" in n for n in names):
                imports.append(f"{node.module}: {names} (line {node.lineno})")
        elif isinstance(node, ast.Import):
            imports.extend(a.name for a in node.names if a.name.startswith("recertia.graph.engine"))
    assert drives == []
    assert imports == []
