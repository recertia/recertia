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
    _backup(root, output)


@backup_app.command("create")
def backup_create(
    root: Path = typer.Option(Path(".recertia"), "--root"),
    output: Optional[Path] = typer.Option(None, "--output"),
) -> None:
    """Create a gzip tar of ``--root``."""

    _backup(root, output)


@backup_app.command("verify")
def backup_verify(
    archive: Path = typer.Argument(..., help="Backup .tar.gz"),
) -> None:
    """Recompute MANIFEST.json checksums. Refuses a tampered archive."""

    from recertia.ops.backup import BackupError, verify_archive

    try:
        manifest = verify_archive(archive)
    except BackupError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    count = len(manifest.get("files", []))
    typer.echo(f"backup OK: {count} files")


def _backup(root: Path, output: Path | None) -> None:
    from recertia.ops.backup import BackupError, backup_tree, default_archive_name

    archive = output if output is not None else Path("backups") / default_archive_name()
    try:
        dest = backup_tree(root, archive)
    except BackupError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(str(dest))


def restore_cmd(
    archive: Path = typer.Argument(..., exists=True, help="Backup .tar.gz"),
    dest: Path = typer.Option(Path(".recertia-restore"), "--dest"),
    overwrite: bool = typer.Option(False, "--overwrite"),
) -> None:
    """Extract a backup archive. Refuses path-escaping members."""

    from recertia.ops.backup import BackupError, restore_tree

    try:
        path = restore_tree(archive, dest, overwrite=overwrite)
    except BackupError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(str(path))
