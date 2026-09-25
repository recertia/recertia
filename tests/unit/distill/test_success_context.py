from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from contracts.applicability import EnvironmentModel
from contracts.criteria import TaskCriterion
from contracts.run import RunState, Task
from recertia.distill.success import distill_success


def test_distill_injects_environment_and_locked_criteria(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "output.txt").write_text("ok\n", encoding="utf-8")
    state = RunState(
        run_id="distill-run",
        task=Task(
            task_id="t",
            request="write a marker file named output.txt in the workspace",
            task_class="repo-chore",
            submitted_at=datetime.now(timezone.utc),
        ),
        criteria=[
            TaskCriterion(
                id="output-exists",
                kind="command",
                run="test -f output.txt",
                source="caller",
                weight=1.0,
            )
        ],
    )
    env = EnvironmentModel(tools=["shell", "edit_file"], backend="local")
    draft, _facts, verdict = distill_success(
        state,
        workdir=workdir,
        commands=["printf ok > output.txt"],
        environment=env,
        locked_criteria=list(state.criteria),
        task_class_sightings=3,
    )
    assert verdict.verdict == "reusable"
    assert draft is not None
    assert draft.failure_modes
    assert len(draft.failure_modes[0].symptom) >= 12
    assert any(p.kind == "tool_available" and p.value == "shell" for p in draft.preconditions)
    assert "local" in draft.intent
    assert draft.certification_criteria[0].id == "output-exists"
    assert draft.certification_criteria[0].run == "test -f output.txt"
