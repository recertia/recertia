"""Eval suite, canary, metrics, lift, and probes. CLI and HTTP share this service."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from recertia.app.authz import Resource
from recertia.ports.identity import Principal


@dataclass(frozen=True)
class LiftReport:
    task_class: str
    treatment: Any
    control: Any
    result: Any
    series_kind: str


class EvalService:
    """``pdp.require`` then the eval harness. Routes do not call ``run_eval_suite``."""

    def __init__(self, *, pdp: Any | None = None) -> None:
        self._pdp = pdp

    def run_eval_suite(
        self,
        principal: Principal,
        *,
        task_class: str,
        golden_root: Path,
        skills_root: Path,
        runs_root: Path,
        eval_store: Any,
        snapshot_id: str,
        golden_dir: Path | None = None,
        factory: Any | None = None,
    ) -> Any:
        self._require(principal, "evals.run")
        from recertia.evals.golden import run_eval_suite
        from recertia.review.gate import default_golden_factory

        return run_eval_suite(
            task_class=task_class,
            golden_root=golden_root,
            skills_root=skills_root,
            runs_root=runs_root,
            eval_store=eval_store,
            snapshot_id=snapshot_id,
            golden_dir=golden_dir,
            factory=factory if factory is not None else default_golden_factory(),
        )

    def run_canary(
        self,
        principal: Principal,
        *,
        live: bool = False,
        canary_root: Path | str | None = None,
        model_version: str | None = None,
    ) -> Any:
        self._require(principal, "metrics.read")
        from recertia.evals.canary import run_judge_canary, run_live_verifier_canary

        if live:
            return run_live_verifier_canary(root=canary_root)
        return run_judge_canary(root=canary_root, model_version=model_version)

    def metrics_report(
        self,
        principal: Principal,
        *,
        eval_store: Any,
        skill_store: Any,
        task_class: str = "repo-chore",
        snapshot_id: str | None = None,
        tenant_id: str | None = None,
        canary_root: Path | str | None = None,
        model_version: str | None = None,
    ) -> Any:
        self._require(principal, "metrics.read")
        from recertia.evals.report import assemble_metric_report

        return assemble_metric_report(
            eval_store,
            skill_store=skill_store,
            task_class=task_class,
            snapshot_id=snapshot_id,
            tenant_id=tenant_id,
            canary_root=canary_root,
            model_version=model_version,
        )

    def metrics_dashboard(
        self,
        principal: Principal,
        *,
        tenant_id: str,
        quota_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require(principal, "metrics.read")
        from recertia.telemetry import get_telemetry, render_dashboard

        payload = render_dashboard(get_telemetry(), tenant_id=tenant_id)
        if quota_snapshot is not None:
            payload["panels"].append(
                {"id": "tenant_quota", "type": "stat", "values": quota_snapshot}
            )
        return payload

    def lift(
        self,
        principal: Principal,
        *,
        eval_store: Any,
        task_class: str,
        snapshot_id: str | None = None,
        min_independent_runs: int,
    ) -> LiftReport:
        self._require(principal, "metrics.read")
        from contracts.eval import BinomialSample
        from recertia.evals.statistics import causal_lift

        counts = eval_store.arm_counts(task_class=task_class, snapshot_id=snapshot_id)
        treatment = counts.get("treatment", BinomialSample(successes=0, trials=0))
        control = counts.get("control", BinomialSample(successes=0, trials=0))
        t_rates, c_rates, paired, series_kind = eval_store.variance_inputs(
            task_class=task_class, snapshot_id=snapshot_id
        )
        result = causal_lift(
            treatment,
            control,
            task_class=task_class,
            snapshot_id=snapshot_id,
            min_independent_runs=min_independent_runs,
            treatment_rates=t_rates or None,
            control_rates=c_rates or None,
            paired_lifts=paired,
        )
        return LiftReport(
            task_class=task_class,
            treatment=treatment,
            control=control,
            result=result,
            series_kind=series_kind,
        )

    def probes(
        self,
        principal: Principal,
        *,
        probes_path: Path,
        skills_root: Path,
        index_path: Path | None = None,
        workdir_root: Path | None = None,
        task_class: str = "repo-chore",
    ) -> Any:
        self._require(principal, "evals.run")
        from recertia.evals.probes import run_probes

        return run_probes(
            probes_path=probes_path,
            skills_root=skills_root,
            index_path=index_path,
            workdir_root=workdir_root,
            task_class=task_class,
        )

    def faithfulness(
        self,
        principal: Principal,
        *,
        skill: Any,
        baseline: Any,
        baseline_event_groups: dict[str, list[str]],
        outcomes: Any,
        event_groups: Any,
        donor: Any | None,
        skill_used: bool,
        min_independent_runs: int,
    ) -> Any:
        self._require(principal, "evals.run")
        from recertia.evals.faithfulness import evaluate_faithfulness

        return evaluate_faithfulness(
            skill=skill,
            baseline=baseline,
            baseline_event_groups=baseline_event_groups,
            outcomes=outcomes,
            event_groups=event_groups,
            donor=donor,
            skill_used=skill_used,
            min_independent_runs=min_independent_runs,
        )

    def suite_payload(self, report: Any) -> dict[str, Any]:
        return {
            "all_passed": report.all_passed,
            "results": [
                {
                    "skill_id": r.skill_id,
                    "passed": r.passed,
                    "terminal": r.terminal,
                    "run_id": r.run_id,
                    "detail": r.detail,
                }
                for r in report.results
            ],
        }

    def _require(self, principal: Principal, action: str) -> None:
        pdp = self._pdp
        if pdp is None:
            return
        pdp.require(principal, action, Resource(tenant_id=principal.tenant_id))
