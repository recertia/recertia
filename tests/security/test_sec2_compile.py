"""file_exists and file_contains compile to confined assertions (SEC-2)."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from contracts.goal import DesiredState, Goal, compile_goal
from recertia.validation.assertions import evaluate_assertion
from recertia.validation.freeze import _freeze_script


def test_file_exists_compiles_to_assertion_not_shell() -> None:
    criteria = compile_goal(
        Goal(desired=[DesiredState(id="ec", kind="file_exists", path=".editorconfig")])
    )
    assert criteria[0].kind == "assertion"
    assert criteria[0].expr == "file_exists('.editorconfig')"
    assert criteria[0].run is None
    assert "test -f" not in (criteria[0].expr or "")


def test_file_contains_compiles_to_file_contains_call() -> None:
    criteria = compile_goal(
        Goal(
            desired=[
                DesiredState(
                    id="py",
                    kind="file_contains",
                    path=".editorconfig",
                    pattern=r"[*.py]",
                )
            ]
        )
    )
    assert criteria[0].kind == "assertion"
    assert criteria[0].expr == "file_contains('.editorconfig', '[*.py]')"
    assert "grep" not in (criteria[0].expr or "")


def test_file_exists_assertion_is_confined(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("ok", encoding="utf-8")
    assert evaluate_assertion("file_exists('marker.txt')", workdir=tmp_path)
    assert evaluate_assertion("file_exists('missing.txt')", workdir=tmp_path) is False
    with pytest.raises(Exception):
        evaluate_assertion("file_exists('../outside')", workdir=tmp_path)


def test_file_contains_matches_inside_the_workdir(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello recertia\n", encoding="utf-8")
    assert evaluate_assertion("file_contains('note.txt', 'recertia')", workdir=tmp_path)
    assert evaluate_assertion("file_contains('note.txt', 'absent')", workdir=tmp_path) is False


def test_freeze_script_is_shlex_quoted() -> None:
    script = _freeze_script({"frozen.txt": "abc"})
    assert script.startswith("python -c ")
    quoted = script.removeprefix("python -c ")
    parts = shlex.split(script)
    assert parts[0:2] == ["python", "-c"]
    assert "frozen.txt" in parts[2]
    assert quoted.startswith("'") or quoted.startswith('"')
