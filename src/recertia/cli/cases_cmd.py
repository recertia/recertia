"""CLI: show episodic cases."""

from __future__ import annotations

import json
from pathlib import Path

import typer

cases_app = typer.Typer(help="Episodic cases (read-only).")


def register_cases_commands(app: typer.Typer) -> None:
    app.add_typer(cases_app, name="cases")


@cases_app.command("show")
def cases_show(
    ctx: typer.Context,
    case_id: str = typer.Argument(...),
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
    tenant: str = typer.Option("default", "--tenant"),
) -> None:
    """Print one case record by case_id."""

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.library import LibraryService
    from recertia.errors import RecertiaError
    from recertia.memory.episodic import EpisodicStore
    from recertia.ports.identity import Principal

    principal = (
        ctx.obj["principal"]
        if isinstance(ctx.obj, dict) and ctx.obj.get("principal") is not None
        else Principal.local_operator()
    )
    store = EpisodicStore(runs_root / "runs" / tenant / "episodic")
    try:
        rec = LibraryService(pdp=PolicyDecisionPoint.load(surface="cli")).get_case(
            principal, store=store, case_id=case_id
        )
    except RecertiaError as exc:
        typer.echo("case not found", err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    typer.echo(json.dumps(rec.model_dump(mode="json"), indent=2))
