"""CLI: recertia trajectory import (Phase 0). Never promotes."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from pydantic import ValidationError

trajectory_app = typer.Typer(help="Import external trajectories (TrajectoryImport).")


def register_trajectory_commands(app: typer.Typer) -> None:
    app.add_typer(trajectory_app, name="trajectory")


@trajectory_app.command("import")
def trajectory_import_cmd(
    path: Path = typer.Argument(..., exists=True, readable=True, dir_okay=False),
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
    tenant: str = typer.Option("default", "--tenant"),
) -> None:
    """Validate and ingest a TrajectoryImport JSON document. Does not promote."""

    from recertia.trajectory.import_store import ImportRejected, ingest_trajectory

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        result = ingest_trajectory(payload, runs_root=runs_root, tenant_id=tenant)
    except (ImportRejected, ValidationError, ValueError, json.JSONDecodeError) as exc:
        typer.echo(json.dumps({"ok": False, "rejected": str(exc)}, indent=2), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "import_id": result.import_id,
                "case_id": result.case_id,
                "stored_path": result.stored_path,
                "reexecutable": result.reexecutable,
                "may_promote": result.may_promote,
                "promote_reason": result.promote_reason,
                "proposal_id": result.proposal_id,
                "promoted": False,
            },
            indent=2,
        )
    )


@trajectory_app.command("distill")
def trajectory_distill_cmd(
    path: Path = typer.Argument(..., exists=True, readable=True, dir_okay=False),
    skills_root: Path = typer.Option(Path("skills"), "--skills-root"),
    actor: str = typer.Option("cli", "--actor"),
    task_class: str = typer.Option(
        ...,
        "--task-class",
        help="Snake-case computer-use class (bug_reproduction|playtest_operator|docs_auditor).",
    ),
) -> None:
    """Author a candidate from a reexecutable import. Does not promote."""

    from recertia.distill.imported import DistillRejected, distill_imported_file
    from recertia.memory.procedural.store import SkillStore

    store = SkillStore(skills_root)
    try:
        version = distill_imported_file(path, store, actor=actor, task_class=task_class)
    except (DistillRejected, ValueError) as exc:
        typer.echo(json.dumps({"ok": False, "rejected": str(exc)}, indent=2), err=True)
        raise typer.Exit(code=1) from exc
    status = store.get_status(version.skill_id, version.version)
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "skill_id": version.skill_id,
                "version": version.version,
                "lifecycle": status.lifecycle if status is not None else None,
                "active": status.active if status is not None else False,
                "task_class": version.task_class,
                "promoted": False,
            },
            indent=2,
        )
    )
