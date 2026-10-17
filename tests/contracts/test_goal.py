"""Variant B: Goal contract, compilation, and hard-criterion invariant."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from contracts.goal import Constraint, DesiredState, Goal, compile_goal
from contracts.run import Task


def test_goal_requires_hard_non_judge_desired():
    with pytest.raises(ValidationError, match="non-judge"):
        Goal(
            desired=[
                DesiredState(
                    id="j1",
                    kind="judge",
                    rubric="looks good",
                    weight=1.0,
                )
            ]
        )


def test_goal_file_exists_compiles_to_command_criterion():
    goal = Goal(
        desired=[
            DesiredState(id="ec", kind="file_exists", path=".editorconfig"),
            DesiredState(
                id="py",
                kind="file_contains",
                path=".editorconfig",
                pattern=r"\[\*\.py\]",
            ),
        ],
        context="Add a root EditorConfig with Python indent settings",
        task_class="repo-chore",
    )
    criteria = compile_goal(goal)
    assert len(criteria) == 2
    assert criteria[0].id == "ec"
    assert criteria[0].kind == "assertion"
    assert criteria[0].expr == "file_exists('.editorconfig')"
    assert criteria[1].kind == "assertion"
    assert criteria[1].expr is not None and criteria[1].expr.startswith("file_contains(")
    assert criteria[0].source == "caller"
    assert all(c.preregistered for c in criteria)


def test_budget_and_no_external_constraints_do_not_become_criteria():
    goal = Goal(
        desired=[DesiredState(id="f", kind="file_exists", path="out.txt")],
        constraints=[
            Constraint(id="cost", kind="budget_ceiling", value=1.5),
            Constraint(id="safe", kind="no_external_effects", value="true"),
            Constraint(id="tests", kind="must_pass_command", value="pytest -q"),
        ],
    )
    criteria = compile_goal(goal)
    ids = {c.id for c in criteria}
    assert "f" in ids
    assert "tests" in ids
    assert "cost" not in ids
    assert "safe" not in ids


def test_task_accepts_goal_without_request():
    goal = Goal(desired=[DesiredState(id="f", kind="file_exists", path="x")])
    t = Task(
        task_id="t1",
        goal=goal,
        submitted_at=datetime.now(timezone.utc),
    )
    assert t.goal is not None
    assert t.request is None


def test_task_rejects_empty_goal_and_empty_request():
    with pytest.raises(ValidationError, match="goal or non-empty request"):
        Task(task_id="t1", submitted_at=datetime.now(timezone.utc))


def test_task_still_accepts_legacy_request_only():
    t = Task(
        task_id="t1",
        request="do the thing",
        submitted_at=datetime.now(timezone.utc),
    )
    assert t.request == "do the thing"
    assert t.goal is None
