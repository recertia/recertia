"""CLI: soak-week log. Never declares operator GA."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

soak_app = typer.Typer(help="Record and inspect soak weeks (does not declare GA).")


def register_soak_commands(app: typer.Typer) -> None:
    app.add_typer(soak_app, name="soak")


@soak_app.command("record")
def soak_record(
    metrics: Path = typer.Option(..., "--metrics", help="weekly-metrics.json"),
    log_path: Path = typer.Option(Path(".recertia/soak-log.json"), "--log"),
    week: Optional[str] = typer.Option(None, "--week", help="ISO week, e.g. 2026-W33."),
    probes: Optional[Path] = typer.Option(None, "--probes"),
    canary: Optional[Path] = typer.Option(None, "--canary"),
) -> None:
    """Append a week. Empty-eval-DB payloads are recorded and not counted."""

    from recertia.ops.soak import classify_week, load_log, read_json, record_week, write_log

    payload = read_json(metrics)
    probe_payload = read_json(probes) if probes is not None else None
    canary_payload = read_json(canary) if canary is not None else None
    classified = classify_week(
        payload, week=week, probes=probe_payload, canary=canary_payload
    )
    log = record_week(load_log(log_path), classified)
    write_log(log_path, log)
    typer.echo(json.dumps(classified.as_dict(), indent=2))
    if not classified.counted:
        typer.echo(f"not counted: {classified.reason}", err=True)


@soak_app.command("status")
def soak_status(
    log_path: Path = typer.Option(Path(".recertia/soak-log.json"), "--log"),
    tabletop: Optional[Path] = typer.Option(None, "--tabletop"),
) -> None:
    """Print consecutive counted weeks and whether the ops gate is ready."""

    from recertia.ops.soak import load_log, read_json, status

    top = read_json(tabletop) if tabletop is not None else None
    payload = status(load_log(log_path), tabletop=top)
    typer.echo(json.dumps(payload, indent=2))
    if payload.get("ga_claimed"):
        typer.echo("soak must not claim GA", err=True)
        raise typer.Exit(code=2)
    if not payload["gate_ready"]:
        raise typer.Exit(code=1)
