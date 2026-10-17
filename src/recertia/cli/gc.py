"""CLI: garbage-collect aged run artifacts."""

from __future__ import annotations

from pathlib import Path

import typer


def register_gc_commands(app: typer.Typer) -> None:
    app.command("gc")(gc_cmd)


def gc_cmd(
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
    older_than_days: float = typer.Option(
        14.0, "--older-than-days", help="Delete artifacts older than this many days."
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Remove aged snapshots, transcripts, and workspaces under ``--runs-root``."""

    from recertia.retention import garbage_collect

    live = _nonterminal_run_ids(runs_root)
    report = garbage_collect(
        runs_root,
        older_than_days=older_than_days,
        dry_run=dry_run,
        live_run_ids=live,
    )
    typer.echo(
        f"gc{' (dry-run)' if dry_run else ''}: "
        f"snapshots={len(report.snapshots)} "
        f"transcripts={len(report.transcripts)} "
        f"workspaces={len(report.workspaces)} "
        f"skipped_live={len(report.skipped_live)} "
        f"total={report.total}"
    )
    for label, items in (
        ("snapshot", report.snapshots),
        ("transcript", report.transcripts),
        ("workspace", report.workspaces),
    ):
        for name in items:
            typer.echo(f"  {label}: {name}")


def _nonterminal_run_ids(runs_root: Path) -> set[str]:
    db = Path(runs_root) / "checkpoints.db"
    if not db.exists():
        return set()
    from recertia.graph.store import CheckpointStore

    store = CheckpointStore(db)
    try:
        return store.nonterminal_run_ids()
    finally:
        store.close()
