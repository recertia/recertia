"""Assemble an operator MetricReport from eval + skill stores (CLI / API / weekly)."""

from __future__ import annotations

from pathlib import Path

from contracts.eval import MetricReport
from recertia.evals.canary import run_judge_canary
from recertia.evals.metrics import build_metric_report, library_yield_inputs
from recertia.evals.store import EvalStore
from recertia.memory.procedural.active_set import recompute_active_set
from recertia.memory.procedural.composition import mean_composition_depth
from recertia.memory.procedural.store import SkillStore
from recertia.review.autonomy_config import DEFAULT_AUTONOMY


def assemble_metric_report(
    eval_store: EvalStore,
    *,
    skill_store: SkillStore,
    task_class: str = "repo-chore",
    snapshot_id: str | None = None,
    model_version: str | None = None,
    canary_root: Path | str | None = None,
    tenant_id: str | None = None,
) -> MetricReport:
    """Build a report with honest ``unavailable`` holes, including yield/precision/decay."""

    from recertia.policy_load import load_policy

    rows = eval_store.metric_rows(
        task_class=task_class, snapshot_id=snapshot_id, tenant_id=tenant_id
    )
    snap = snapshot_id or (rows[0]["snapshot_id"] if rows else "none")
    min_runs = load_policy().min_independent_runs
    _updated, pressure = recompute_active_set(skill_store, config=DEFAULT_AUTONOMY)
    mean_pressure = sum(pressure.values()) / len(pressure) if pressure else 0.0
    canary = run_judge_canary(root=canary_root, model_version=model_version)
    ever_benched = sum(
        1
        for _v, status, _s in skill_store.iter_loaded()
        if status.retirement.benched_at is not None or status.lifecycle == "benched"
    )
    restored = sum(
        1
        for _v, status, _s in skill_store.iter_loaded()
        if status.retirement.restored_at is not None
    )
    approved_ids = {
        version.skill_id
        for version, status, _stats in skill_store.iter_loaded()
        if status.lifecycle == "approved"
    }
    applied, total = library_yield_inputs(rows, approved_ids=approved_ids)
    probes = eval_store.list_probe_snapshots(task_class=task_class, limit=2)
    precision = probes[0]["precision_at_3"] if probes else None
    prior = probes[1]["precision_at_3"] if len(probes) >= 2 else None
    skills_added = None
    if len(probes) >= 2:
        skills_added = int(probes[0]["skill_count"]) - int(probes[1]["skill_count"])
    return build_metric_report(
        rows,
        snapshot_id=snap,
        task_class=task_class,
        model_version=model_version,
        active_cap_pressure=mean_pressure,
        judge_false_pass_rate=canary.false_pass_rate,
        mean_composition_depth=mean_composition_depth(skill_store),
        retirement_benched=ever_benched if ever_benched else None,
        retirement_restored=restored if ever_benched else None,
        approved_applied=applied,
        approved_total=total if approved_ids else 0,
        precision_at_3=precision,
        prior_precision_at_3=prior,
        skills_added=skills_added,
        min_independent_runs=min_runs,
    )


def weekly_claim(report: MetricReport) -> str:
    """Operator-facing lift claim. A CI that spans zero is never an improvement."""

    lift = report.causal_lift
    if lift is None:
        return report.unavailable.get("causal_lift", "insufficient_data")
    interval = lift.interval
    if interval is not None and interval.low <= 0 <= interval.high:
        return "not established"
    if lift.status == "not_established":
        return "not established"
    if lift.status == "low_run_count":
        return "low run count"
    if lift.status == "insufficient_data":
        return "insufficient_data"
    return lift.status
