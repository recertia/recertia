"""CLI: garbage-collect aged run artifacts."""

from __future__ import annotations

from pathlib import Path

import typer


def register_gc_commands(app: typer.Typer) -> None:
    app.command("gc")(gc_cmd)


def gc_cmd(
    ctx: typer.Context,
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
    older_than_days: float = typer.Option(
        14.0, "--older-than-days", help="Delete artifacts older than this many days."
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Remove aged snapshots, transcripts, and workspaces under ``--runs-root``."""

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.ops_service import OpsService
    from recertia.errors import RecertiaError
    from recertia.ports.identity import Principal

    principal = (
        ctx.obj["principal"]
        if isinstance(ctx.obj, dict) and ctx.obj.get("principal") is not None
        else Principal.local_operator()
    )
    try:
        report = OpsService(pdp=PolicyDecisionPoint.load(surface="cli")).gc(
            principal, root=runs_root, older_than_days=older_than_days, dry_run=dry_run
        )
    except RecertiaError as exc:
        from recertia.cli.exit import raise_exit

        raise_exit(exc)
    typer.echo(
        f"gc{' (dry-run)' if dry_run else ''}: "
        f"snapshots={len(report.snapshots)} "
        f"transcripts={len(report.transcripts)} "
        f"workspaces={len(report.workspaces)} "
        f"total={report.total}"
    )
    for label, items in (
        ("snapshot", report.snapshots),
        ("transcript", report.transcripts),
        ("workspace", report.workspaces),
    ):
        for name in items:
            typer.echo(f"  {label}: {name}")
