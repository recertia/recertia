"""CLI and API promote, eval, canary, and metrics go through the services."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from recertia.app.evals import EvalService
from recertia.app.promotion import PromotionService
from recertia.errors import Forbidden
from recertia.ports.identity import Principal

ROOT = Path(__file__).resolve().parents[3]


def test_cli_and_api_call_promotion_and_eval_services() -> None:
    cli_skills = (ROOT / "src/recertia/cli/skills.py").read_text(encoding="utf-8")
    cli_eval = (ROOT / "src/recertia/cli/eval_cmd.py").read_text(encoding="utf-8")
    cli_metrics = (ROOT / "src/recertia/cli/metrics_cmd.py").read_text(encoding="utf-8")
    cli_canary = (ROOT / "src/recertia/cli/canary_cmd.py").read_text(encoding="utf-8")
    api_lib = (ROOT / "src/recertia/api/console_library_routes.py").read_text(encoding="utf-8")
    api_rem = (ROOT / "src/recertia/api/remaining_routes.py").read_text(encoding="utf-8")
    api_app = (ROOT / "src/recertia/api/__init__.py").read_text(encoding="utf-8")
    assert "PromotionService" in cli_skills
    assert "service.promote" in cli_skills
    assert "promote_to_approved(" not in cli_skills
    assert "EvalService" in cli_eval
    assert "run_eval_suite(" not in cli_eval.replace("service.run_eval_suite(", "")
    assert "EvalService" in cli_metrics
    assert "assemble_metric_report" not in cli_metrics
    assert "EvalService" in cli_canary
    assert "run_judge_canary" not in cli_canary
    assert "promotion_service.promote" in api_lib
    assert "promote_to_approved(" not in api_lib
    assert "eval_service.metrics_report" in api_lib
    assert "eval_service.run_canary" in api_lib
    assert "eval_service.run_eval_suite" in api_rem
    assert "eval_service.metrics_dashboard" in api_app


def test_promote_requires_promote_scope() -> None:
    class _Pdp:
        def require(self, principal: Principal, action: str, resource: Any) -> None:
            del resource
            if action != "skills.promote" or not principal.has_scope("promote"):
                raise Forbidden("missing scope: promote")

    service = PromotionService(pdp=_Pdp(), gate=SimpleNamespace())
    runs = Principal.from_api_key(
        key_id="k", tenant_id="t1", scopes=frozenset({"runs"})
    )
    with pytest.raises(Forbidden, match="promote"):
        service.promote(
            runs,
            "demo",
            1,
            store=SimpleNamespace(),
            runs_root=Path("."),
            log_dir=Path("."),
        )


def test_promote_passes_injected_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = SimpleNamespace(name="injected")
    captured: dict[str, Any] = {}

    def _promote(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        captured["gate"] = kwargs.get("gate")
        return SimpleNamespace(lifecycle="approved", active=True, skill_id="demo", version=1)

    monkeypatch.setattr("recertia.app.promotion.promote_to_approved", _promote)
    service = PromotionService(pdp=None, gate=gate)  # type: ignore[arg-type]
    operator = Principal.local_operator()
    status = service.promote(
        operator,
        "demo",
        1,
        store=SimpleNamespace(),
        runs_root=Path("."),
        log_dir=Path("."),
    )
    assert captured["gate"] is gate
    assert status.lifecycle == "approved"


def test_eval_suite_requires_evals_or_metrics_scope() -> None:
    class _Pdp:
        def require(self, principal: Principal, action: str, resource: Any) -> None:
            del resource
            if action != "evals.run" or not (
                principal.has_scope("evals") or principal.has_scope("metrics")
            ):
                raise Forbidden("missing scope: evals")

    service = EvalService(pdp=_Pdp())
    runs = Principal.from_api_key(
        key_id="k", tenant_id="t1", scopes=frozenset({"runs"})
    )
    with pytest.raises(Forbidden, match="evals"):
        service.run_eval_suite(
            runs,
            task_class="repo-chore",
            golden_root=Path("evals/golden"),
            skills_root=Path("skills"),
            runs_root=Path("."),
            eval_store=SimpleNamespace(),
            snapshot_id="x",
        )
