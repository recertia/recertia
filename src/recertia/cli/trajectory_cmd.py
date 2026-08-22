"""CLI: import external trajectories (ADR-0019). Never promotes."""

from __future__ import annotations

import json
from pathlib import Path

import typer

trajectory_app = typer.Typer(help="Import external trajectories into episodic memory.")


def register_trajectory_commands(app: typer.Typer) -> None:
    app.add_typer(trajectory_app, name="trajectory")


@trajectory_app.command("import")
def trajectory_import_cmd(
    path: Path = typer.Argument(..., exists=True, readable=True, dir_okay=False),
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
    tenant: str = typer.Option("default", "--tenant"),
    actor: str = typer.Option("cli", "--actor"),
) -> None:
    """Validate and ingest a TrajectoryImport JSON document. Does not promote."""

    from recertia.trajectory.import_store import ImportRejected, ingest_trajectory

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        result = ingest_trajectory(
            payload, runs_root=runs_root, tenant_id=tenant, actor=actor
        )
    except (ImportRejected, ValueError, json.JSONDecodeError) as exc:
        typer.echo(f"rejected: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        json.dumps(
            {
                "import_id": result.import_id,
                "case_id": result.case_id,
                "stored_path": result.stored_path,
                "proposal_id": result.proposal_id,
                "reexecutable": result.reexecutable,
                "promoted": result.promoted,
            },
            indent=2,
        )
    )
