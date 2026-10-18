"""CLI: labelled retrieval probes (remaining-work RW-M2)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

probes_app = typer.Typer(help="Run labelled retrieval probes.")


def register_probes_commands(app: typer.Typer) -> None:
    app.add_typer(probes_app, name="probes")


@probes_app.command("run")
def probes_run(
    ctx: typer.Context,
    probes: Path = typer.Option(Path("evals/probes/repo-chore.json"), "--probes"),
    skills_root: Path = typer.Option(Path("skills"), "--skills-root"),
    eval_db: Path = typer.Option(Path(".recertia/evals.db"), "--eval-db"),
    index_path: Optional[Path] = typer.Option(None, "--index"),
    workdir_root: Optional[Path] = typer.Option(None, "--workdir-root"),
    task_class: str = typer.Option("repo-chore", "--task-class"),
    persist: bool = typer.Option(True, "--persist/--no-persist"),
    output: Optional[Path] = typer.Option(None, "--output"),
    min_precision: float = typer.Option(0.7, "--min-precision"),
) -> None:
    """Run probes through retrieve; persist precision@3; fail if mean < floor."""

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
    try:
        result = service.probes(
            principal,
            probes_path=probes,
            skills_root=skills_root,
            index_path=index_path,
            workdir_root=workdir_root,
            task_class=task_class,
        )
    except RecertiaError as exc:
        from recertia.cli.exit import raise_exit

        raise_exit(exc)
    if persist:
        store = EvalStore(eval_db)
        try:
            store.record_probe_snapshot(
                task_class=result.task_class,
                snapshot_id=result.snapshot_id,
                precision_at_3=result.precision_at_3,
                skill_count=result.skill_count,
                payload=result.as_payload(),
                recorded_at=result.recorded_at,
            )
        finally:
            store.close()
    payload = result.as_payload()
    text = json.dumps(payload, indent=2)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    typer.echo(text)
    if not result.probes:
        typer.echo("no probes in set", err=True)
        raise typer.Exit(code=1)
    if result.precision_at_3 < min_precision:
        typer.echo(
            f"retrieval_precision_at_3={result.precision_at_3:.3f} < {min_precision}",
            err=True,
        )
        raise typer.Exit(code=1)
