"""CLI: run golden eval suite and write EvalObservation rows."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

eval_app = typer.Typer(help="Eval harness (golden fixtures, eval firewall).")


def register_eval_commands(app: typer.Typer) -> None:
    app.add_typer(eval_app, name="eval")


@eval_app.command("run")
def eval_run(
    ctx: typer.Context,
    task_class: str = typer.Option("repo-chore", "--task-class"),
    golden_root: Path = typer.Option(Path("evals/golden"), "--golden-root"),
    golden_dir: Optional[Path] = typer.Option(None, "--golden-dir"),
    skills_root: Path = typer.Option(Path("skills"), "--skills-root"),
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
    eval_db: Path = typer.Option(Path(".recertia/evals.db"), "--eval-db"),
    snapshot_id: str = typer.Option("eval-cli", "--snapshot-id"),
) -> None:
    """Run golden fixtures with the eval firewall; append observations."""

    import json

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.evals import EvalService
    from recertia.errors import RecertiaError
    from recertia.evals.store import EvalStore
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
            report = service.run_eval_suite(
                principal,
                task_class=task_class,
                golden_root=golden_root,
                skills_root=skills_root,
                runs_root=runs_root,
                eval_store=store,
                snapshot_id=snapshot_id,
                golden_dir=golden_dir,
            )
        except RecertiaError as exc:
            typer.echo(f"{exc.code}: {exc.message}", err=True)
            raise typer.Exit(code=exc.exit_code) from exc
    finally:
        store.close()
    payload = service.suite_payload(report)
    typer.echo(json.dumps(payload, indent=2))
    if not report.all_passed:
        raise typer.Exit(code=1)
