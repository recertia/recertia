"""Structural lock for the T2 planted-regression set (REC-013). Does not run the gate."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[3]
SET_DIR = REPO / "evals" / "planted-regressions" / "t2"
SKILLS = REPO / "skills"
CASES = (
    "add-editorconfig",
    "add-gitignore-entry",
    "add-license-mit",
    "add-makefile-target",
    "add-pytest-config",
    "add-readme-section",
    "bump-action-checkout",
    "ensure-src-layout",
    "pin-python-version",
    "strip-trailing-whitespace",
)


def test_ten_cases_are_steps_only_v2_successors() -> None:
    for skill_id in CASES:
        v1 = json.loads((SKILLS / skill_id / "v1" / "version.json").read_text(encoding="utf-8"))
        v2 = json.loads((SET_DIR / skill_id / "version.json").read_text(encoding="utf-8"))
        assert v2["skill_id"] == skill_id
        assert v2["version"] == 2
        assert v2["supersedes"] == 1
        assert v1["certification_criteria"] == v2["certification_criteria"]
        assert v1["steps"][0]["inputs"]["command"] != v2["steps"][0]["inputs"]["command"]
        skipped = {"version", "supersedes", "steps"}
        for key, value in v1.items():
            if key in skipped:
                continue
            assert v2[key] == value, f"{skill_id} changed {key}"
        diff = (SET_DIR / skill_id / "criteria.diff").read_text(encoding="utf-8")
        assert "--- v1 certification_criteria" in diff
        assert "+++" in diff
        assert "@@" not in diff


def test_manifest_covers_the_set_except_itself() -> None:
    manifest = SET_DIR / "MANIFEST.sha256"
    assert manifest.is_file()
    listed = {
        line.split("  ", 1)[1].strip()
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    on_disk = {
        path.relative_to(SET_DIR).as_posix()
        for path in SET_DIR.rglob("*")
        if path.is_file() and path.name != "MANIFEST.sha256"
    }
    assert listed == on_disk
    assert "PREFREEZE-REFUSALS.json" in listed
    for skill_id in CASES:
        assert f"{skill_id}/version.json" in listed
        assert f"{skill_id}/criteria.diff" in listed


def test_prefreeze_refusals_are_allowed_kinds_on_container() -> None:
    payload = json.loads((SET_DIR / "PREFREEZE-REFUSALS.json").read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in payload["cases"]}
    assert tuple(by_id) == CASES or set(by_id) == set(CASES)
    allowed = {"golden_gate", "predecessor_non_regression"}
    for skill_id in CASES:
        row = by_id[skill_id]
        assert row["refusal_kind"] in allowed, skill_id
        assert row["backend_used"] == "container", skill_id
        assert row["criteria_diff"] == "identical", skill_id
        if row.get("attempts_measured") is True:
            assert isinstance(row["attempts"], int), skill_id
            assert row["attempts"] >= 1, skill_id
        else:
            assert row["attempts"] is None, skill_id


def _load_prefreeze() -> ModuleType:
    spec = importlib.util.spec_from_file_location("t2_prefreeze", SET_DIR / "prefreeze.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # No __pycache__ in the frozen set: the manifest test compares every file on disk.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def test_prefreeze_refusals_do_not_present_attempts_as_a_measurement() -> None:
    """The pre-freeze run never measured attempts; no row may read as if it had (REC-013)."""

    payload = json.loads((SET_DIR / "PREFREEZE-REFUSALS.json").read_text(encoding="utf-8"))
    rows = payload["cases"]
    assert rows
    unmeasured = []
    for row in rows:
        skill_id = row["id"]
        assert "attempts_measured" in row, f"{skill_id}: attempts has no measured/not-measured flag"
        assert isinstance(row["attempts_measured"], bool), skill_id
        if row["attempts_measured"]:
            assert isinstance(row["attempts"], int) and row["attempts"] >= 1, skill_id
        else:
            assert row["attempts"] is None, f"{skill_id}: unmeasured attempts carries a number"
            unmeasured.append(skill_id)
    if unmeasured:
        reason = payload.get("not_measured", {}).get("attempts")
        assert isinstance(reason, str) and reason.strip(), "no top-level not_measured.attempts reason"


def test_prefreeze_case_rows_mark_attempts_not_measured() -> None:
    """Rows the pre-freeze script would write carry no attempt count it did not measure."""

    prefreeze = _load_prefreeze()
    row = prefreeze.case_row(
        skill_id="add-license-mit",
        refusal_kind="predecessor_non_regression",
        backend_used="container",
        criteria_diff="identical",
        executor_id="recertia.solver.executor.ContainerExecutor",
        code_sha="0" * 40,
        date_pt="2026-10-21",
        message="predecessor non-regression failed",
    )
    assert row["attempts"] is None
    assert row["attempts_measured"] is False
    assert prefreeze.case_row_problems(row) == []
    assert prefreeze.case_row_problems({**row, "attempts": 1}) != []


def test_golden_fixtures_match_head() -> None:
    proc = subprocess.run(
        ["git", "diff", "--exit-code", "HEAD", "--", "evals/golden"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
