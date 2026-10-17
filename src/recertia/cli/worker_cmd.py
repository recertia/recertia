"""``recertia worker`` drains the SQLite run queue. Sync API runs stay inline."""

from __future__ import annotations

import time
from pathlib import Path

import typer


def register_worker_commands(app: typer.Typer) -> None:
    app.command("worker")(worker_cmd)


def worker_cmd(
    root: Path = typer.Option(Path(".recertia"), "--root"),
    once: bool = typer.Option(False, "--once", help="Claim and run at most one item, then exit."),
    concurrency: int = typer.Option(1, "--concurrency"),
) -> None:
    """Execute queued runs. The default API path does not wait on this process."""

    from recertia.errors import RecertiaError
    from recertia.settings import load_settings
    from recertia.workers.serve import worker_id

    try:
        load_settings().validate_deployment(role="worker")
    except RecertiaError as exc:
        typer.echo(f"{exc.code}: {exc.message}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc

    ident = worker_id()
    if once:
        _run_once(root, ident, concurrency)
        return
    while True:
        if _run_once(root, ident, concurrency) == 0:
            time.sleep(0.2)


def _run_once(root: Path, ident: str, capacity: int) -> int:
    from recertia.workers.serve import run_once

    try:
        ran = run_once(root, worker=ident, capacity=capacity)
    except Exception as exc:
        typer.echo(f"worker failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo("claimed=0" if ran == 0 else f"claimed=1 worker={ident}")
    return ran
