"""CLI: recertia task-state show / verify (Phase 0 remaining + Phase 3 surface)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

task_state_app = typer.Typer(help="Inspect and verify AuditedTaskState projections (MEA).")


def register_task_state_commands(app: typer.Typer) -> None:
    app.add_typer(task_state_app, name="task-state")


@task_state_app.command("show")
def task_state_show(
    state_path: Path = typer.Argument(..., help="Path to AuditedTaskState JSON"),
    previous: Optional[Path] = typer.Option(
        None, "--previous", help="Prior version for stuck detection"
    ),
    brief: bool = typer.Option(False, "--brief", help="Systems brief only"),
) -> None:
    """Print AuditedTaskState or a Systems brief projection."""

    from recertia.app.checkpoints import CheckpointReader

    reader = CheckpointReader()
    state = reader.load_audited_file(state_path)
    prev = reader.load_audited_file(previous) if previous is not None else None
    if brief:
        b = reader.systems_brief(state, previous=prev)
        typer.echo(
            json.dumps(
                {
                    "state_id": b.state_id,
                    "goal_id": b.goal_id,
                    "version": b.version,
                    "current_phase": b.current_phase,
                    "rounds_consumed": b.rounds_consumed,
                    "rounds_remaining": b.rounds_remaining,
                    "max_rounds": b.max_rounds,
                    "residual_attempts": b.residual_attempts,
                    "evidence_coverage": b.evidence_coverage,
                    "blocker_ids": b.blocker_ids,
                    "stuck": b.stuck,
                    "stuck_reason": b.stuck_reason,
                    "last_auditor_report_id": b.last_auditor_report_id,
                },
                indent=2,
            )
        )
        if b.stuck:
            raise typer.Exit(code=1)
        return
    typer.echo(state.model_dump_json(indent=2))


@task_state_app.command("verify")
def task_state_verify(
    state_path: Path = typer.Argument(..., help="Path to AuditedTaskState JSON"),
) -> None:
    """Validate structure and recoverability invariants of an AuditedTaskState."""

    from recertia.app.checkpoints import CheckpointReader

    reader = CheckpointReader()
    state = reader.load_audited_file(state_path)
    errors = reader.verify_audited(state)
    if errors:
        typer.echo(json.dumps({"ok": False, "errors": errors}, indent=2))
        raise typer.Exit(code=1)
    typer.echo(json.dumps({"ok": True, "state_id": state.state_id, "version": state.version}, indent=2))
