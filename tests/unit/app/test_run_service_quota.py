"""Quota admission has one implementation, and routes call that service."""

from __future__ import annotations

from pathlib import Path

from recertia.app.runs import RunService

API = Path(__file__).resolve().parents[3] / "src" / "recertia" / "api"


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


def test_service_does_not_drive_orchestrator() -> None:
    source = (
        Path(__file__).resolve().parents[3] / "src" / "recertia" / "app" / "runs.py"
    ).read_text(encoding="utf-8")
    assert "orchestrator.start" not in source
    assert "orchestrator.resume" not in source


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
