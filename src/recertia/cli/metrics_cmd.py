"""CLI: emit MetricReport / weekly lift summary for operator cadence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer


def register_metrics_commands(app: typer.Typer) -> None:
    app.command("metrics")(metrics_cmd)


def metrics_cmd(
    ctx: typer.Context,
    task_class: str = typer.Option("repo-chore", "--task-class"),
    eval_db: Path = typer.Option(Path(".recertia/evals.db"), "--eval-db"),
    snapshot_id: Optional[str] = typer.Option(None, "--snapshot-id"),
    skills_root: Path = typer.Option(Path("skills"), "--skills-root"),
    canary_root: Path = typer.Option(
        Path("evals/canary/planted-failure"), "--canary-root"
    ),
    model_version: Optional[str] = typer.Option(None, "--model-version"),
    output: Optional[Path] = typer.Option(None, "--output", help="Write JSON report."),
) -> None:
    """Build a MetricReport from the eval store (honest unavailable reasons when sparse)."""

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.evals import EvalService
    from recertia.errors import RecertiaError
    from recertia.evals.store import EvalStore
    from recertia.memory.procedural.store import SkillStore
    from recertia.ports.identity import Principal

    principal = (
        ctx.obj["principal"]
        if isinstance(ctx.obj, dict) and ctx.obj.get("principal") is not None
        else Principal.local_operator()
    )
    service = EvalService(pdp=PolicyDecisionPoint.load(surface="cli"))
    store = EvalStore(eval_db)
    try:
        try:
            skill_store = SkillStore(skills_root)
            report = service.metrics_report(
                principal,
                eval_store=store,
                skill_store=skill_store,
                task_class=task_class,
                snapshot_id=snapshot_id,
                model_version=model_version,
                canary_root=canary_root,
            )
        except RecertiaError as exc:
            from recertia.cli.exit import raise_exit

            raise_exit(exc)
    finally:
        store.close()

    payload = report.model_dump(mode="json")
    text = json.dumps(payload, indent=2)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    typer.echo(text)
    if report.causal_lift is not None and report.causal_lift.status == "not_established":
        typer.echo("claim=not established (interval includes zero)", err=True)
    elif report.causal_lift is not None and report.causal_lift.status == "low_run_count":
        typer.echo("claim=not established (independent run count below floor)", err=True)
