"""CLI: list the files that belong to one tenant. No crypto-shred."""

from __future__ import annotations

from pathlib import Path

import typer

tenant_app = typer.Typer(help="Tenant data paths.")


def register_tenant_commands(app: typer.Typer) -> None:
    app.add_typer(tenant_app, name="tenant")


@tenant_app.command("export")
def tenant_export(
    tenant: str = typer.Option(..., "--tenant"),
    root: Path = typer.Option(Path(".recertia"), "--root"),
    dry_run: bool = typer.Option(False, "--dry-run", help="List paths. Write nothing."),
) -> None:
    """List paths under a tenant root. Export bytes are not written."""

    if not dry_run:
        typer.echo("pass --dry-run; export does not write or crypto-shred", err=True)
        raise typer.Exit(code=2)

    from recertia.errors import InvalidTenantId
    from recertia.persistence.layout import RuntimeLayout

    layout = RuntimeLayout(root)
    try:
        runs = layout.tenant(tenant)
        blobs = layout.blobs_dir(tenant)
    except InvalidTenantId as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    lines = [runs.runs_root]
    if runs.runs_root.is_dir():
        lines.extend(sorted(path for path in runs.runs_root.rglob("*")))
    lines.append(blobs)
    if blobs.is_dir():
        lines.extend(sorted(path for path in blobs.rglob("*")))
    for path in lines:
        typer.echo(str(path))
