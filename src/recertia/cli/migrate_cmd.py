"""CLI: one-shot migrations that are also run at API startup."""

from __future__ import annotations

from pathlib import Path

import typer

migrate_app = typer.Typer(help="One-shot on-disk migrations.")


def register_migrate_commands(app: typer.Typer) -> None:
    app.add_typer(migrate_app, name="migrate")


@migrate_app.command("run-events")
def migrate_run_events_cmd(
    root: Path = typer.Option(Path(".recertia"), "--root"),
) -> None:
    """Move legacy root/run_events files under root/runs/<tenant>/run_events/."""

    from recertia.persistence.migrate_run_events import migrate_run_events

    report = migrate_run_events(root)
    typer.echo(
        f"moved={len(report.moved)} quarantined={len(report.quarantined)} "
        f"pending={report.pending}"
    )
