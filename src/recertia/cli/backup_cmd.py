"""CLI: backup and restore the ``.recertia/`` durability unit."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

backup_app = typer.Typer(
    help="Backup and restore .recertia/ (RPO ≤ 24h).",
    invoke_without_command=True,
)


def register_backup_commands(app: typer.Typer) -> None:
    app.add_typer(backup_app, name="backup")
    app.command("restore")(restore_cmd)


@backup_app.callback(invoke_without_command=True)
def backup_root(
    ctx: typer.Context,
    root: Path = typer.Option(Path(".recertia"), "--root"),
    output: Optional[Path] = typer.Option(None, "--output", help="Archive path (.tar.gz)."),
) -> None:
    """Create a gzip tar of ``--root``. Default command when no subcommand is given."""

    if ctx.invoked_subcommand is not None:
        return
    _backup(root, output, ctx=ctx)


@backup_app.command("create")
def backup_create(
    ctx: typer.Context,
    root: Path = typer.Option(Path(".recertia"), "--root"),
    output: Optional[Path] = typer.Option(None, "--output"),
) -> None:
    """Create a gzip tar of ``--root``."""

    _backup(root, output, ctx=ctx)


@backup_app.command("verify")
def backup_verify(
    ctx: typer.Context,
    archive: Path = typer.Argument(..., help="Backup .tar.gz"),
) -> None:
    """Recompute MANIFEST.json checksums. Refuses a tampered archive."""

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.ops_service import OpsService
    from recertia.errors import RecertiaError
    from recertia.ops.backup import BackupError
    from recertia.ports.identity import Principal

    principal = (
        ctx.obj["principal"]
        if isinstance(ctx.obj, dict) and ctx.obj.get("principal") is not None
        else Principal.local_operator()
    )
    try:
        manifest = OpsService(pdp=PolicyDecisionPoint.load(surface="cli")).verify_backup(
            principal, archive=archive
        )
    except (BackupError, RecertiaError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    count = len(manifest.get("files", []))
    typer.echo(f"backup OK: {count} files")


def _backup(root: Path, output: Path | None, *, ctx: typer.Context | None = None) -> None:
    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.ops_service import OpsService
    from recertia.errors import RecertiaError
    from recertia.ops.backup import BackupError
    from recertia.ports.identity import Principal

    principal = Principal.local_operator()
    if ctx is not None and isinstance(ctx.obj, dict) and ctx.obj.get("principal") is not None:
        principal = ctx.obj["principal"]
    try:
        dest = OpsService(pdp=PolicyDecisionPoint.load(surface="cli")).backup(
            principal, root=root, output=output
        )
    except (BackupError, RecertiaError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(str(dest))


def restore_cmd(
    ctx: typer.Context,
    archive: Path = typer.Argument(..., exists=True, help="Backup .tar.gz"),
    dest: Path = typer.Option(Path(".recertia-restore"), "--dest"),
    overwrite: bool = typer.Option(False, "--overwrite"),
) -> None:
    """Extract a backup archive. Refuses path-escaping members."""

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.ops_service import OpsService
    from recertia.errors import RecertiaError
    from recertia.ops.backup import BackupError
    from recertia.ports.identity import Principal

    principal = (
        ctx.obj["principal"]
        if isinstance(ctx.obj, dict) and ctx.obj.get("principal") is not None
        else Principal.local_operator()
    )
    try:
        path = OpsService(pdp=PolicyDecisionPoint.load(surface="cli")).restore(
            principal, archive=archive, dest=dest, overwrite=overwrite
        )
    except (BackupError, RecertiaError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(str(path))
