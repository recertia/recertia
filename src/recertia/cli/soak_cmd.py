"""CLI: soak-week log. Never declares operator GA."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

soak_app = typer.Typer(help="Record and inspect soak weeks (does not declare GA).")


def register_soak_commands(app: typer.Typer) -> None:
    app.add_typer(soak_app, name="soak")


def _principal(ctx: typer.Context):
    from recertia.ports.identity import Principal

    return (
        ctx.obj["principal"]
        if isinstance(ctx.obj, dict) and ctx.obj.get("principal") is not None
        else Principal.local_operator()
    )


@soak_app.command("record")
def soak_record(
    ctx: typer.Context,
    metrics: Path = typer.Option(..., "--metrics", help="weekly-metrics.json"),
    log_path: Path = typer.Option(Path(".recertia/soak-log.json"), "--log"),
    week: Optional[str] = typer.Option(None, "--week", help="ISO week, e.g. 2026-W33."),
    probes: Optional[Path] = typer.Option(None, "--probes"),
    canary: Optional[Path] = typer.Option(None, "--canary"),
) -> None:
    """Append a week. Empty-eval-DB payloads are recorded and not counted."""

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.ops_service import OpsService
    from recertia.errors import RecertiaError

    try:
        classified = OpsService(pdp=PolicyDecisionPoint.load(surface="cli")).soak_record(
            _principal(ctx),
            metrics=metrics,
            log_path=log_path,
            week=week,
            probes=probes,
            canary=canary,
        )
    except RecertiaError as exc:
        typer.echo(f"{exc.code}: {exc.message}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    typer.echo(json.dumps(classified.as_dict(), indent=2))
    if not classified.counted:
        typer.echo(f"not counted: {classified.reason}", err=True)


@soak_app.command("status")
def soak_status(
    ctx: typer.Context,
    log_path: Path = typer.Option(Path(".recertia/soak-log.json"), "--log"),
    tabletop: Optional[Path] = typer.Option(None, "--tabletop"),
) -> None:
    """Print consecutive counted weeks and whether the ops gate is ready."""

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.ops_service import OpsService
    from recertia.errors import RecertiaError

    try:
        payload = OpsService(pdp=PolicyDecisionPoint.load(surface="cli")).soak_status(
            _principal(ctx), log_path=log_path, tabletop=tabletop
        )
    except RecertiaError as exc:
        typer.echo(f"{exc.code}: {exc.message}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    typer.echo(json.dumps(payload, indent=2))
    if payload.get("ga_claimed"):
        typer.echo("soak must not claim GA", err=True)
        raise typer.Exit(code=2)
    if not payload["gate_ready"]:
        raise typer.Exit(code=1)
