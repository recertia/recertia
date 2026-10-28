"""CLI: list or copy the files that belong to one tenant. No crypto-shred."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import NoReturn, Optional

import typer

tenant_app = typer.Typer(help="Tenant data paths.")


def register_tenant_commands(app: typer.Typer) -> None:
    app.add_typer(tenant_app, name="tenant")


def _under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _collect(runs_root: Path, blobs: Path) -> list[Path]:
    lines = [runs_root]
    if runs_root.is_dir():
        lines.extend(sorted(path for path in runs_root.rglob("*")))
    lines.append(blobs)
    if blobs.is_dir():
        lines.extend(sorted(path for path in blobs.rglob("*")))
    return lines


def _refuse(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code=2)


@tenant_app.command("export")
def tenant_export(
    tenant: str = typer.Option(..., "--tenant"),
    root: Path = typer.Option(Path(".recertia"), "--root"),
    dest: Optional[Path] = typer.Option(
        None, "--dest", help="Directory under --root or the system temp directory."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List paths and write nothing. This is the default."
    ),
    execute: bool = typer.Option(False, "--execute", help="Copy tenant files into --dest."),
) -> None:
    """List tenant paths. ``--execute`` copies files to ``--dest``. No crypto-shred."""

    if execute and dry_run:
        _refuse("pass either --dry-run or --execute")

    from recertia.errors import InvalidTenantId
    from recertia.persistence.layout import RuntimeLayout

    layout = RuntimeLayout(root)
    try:
        runs = layout.tenant(tenant)
        blobs = layout.blobs_dir(tenant)
    except InvalidTenantId as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    # The runs roots' empty tenant lock files are locks, not tenant data (REC-065), so
    # neither the listing nor the copy includes them: --dry-run predicts --execute.
    lock_files = {runs.tenant_lock, runs.jobs().tenant_lock}
    lines = [path for path in _collect(runs.runs_root, blobs) if path not in lock_files]
    if not execute:
        for path in lines:
            typer.echo(str(path))
        return

    if dest is None:
        _refuse("pass --dest with --execute")
    root_r = root.resolve()
    dest_r = dest.resolve()
    tmp = Path(tempfile.gettempdir()).resolve()
    allowed = dest_r == root_r or _under(dest_r, root_r) or dest_r == tmp or _under(dest_r, tmp)
    if not allowed:
        _refuse("dest must be under the runs root or the system temp directory")
    source_dirs = [runs.runs_root.resolve(), blobs.resolve()]
    if any(dest_r == tree or _under(dest_r, tree) for tree in source_dirs):
        _refuse("dest must not sit inside the source tenant tree")

    copied = 0
    for src in lines:
        if not src.is_file():
            continue
        try:
            rel = src.resolve().relative_to(root_r)
        except ValueError:
            _refuse(f"refusing to copy {src}")
        target = dest_r / rel
        if any(target == tree or _under(target, tree) for tree in source_dirs):
            _refuse("refusing to copy into the source tenant tree")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        typer.echo(str(target))
        copied += 1
    typer.echo(f"copied {copied} files")
