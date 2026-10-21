"""REC-009: the held-out split stays out of the repo and out of golden discovery.

Only the manifest schema (and, from WO-07, one public manifest) may be committed under
``evals/heldout/``. Golden discovery and the plan-acceptance glob are rooted at
``evals/golden/`` and never reach ``evals/heldout/``. All fixtures here are synthetic.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from recertia.evals.golden import discover_golden, list_goldens_for_task_class
from recertia.evals.heldout_manifest import load_heldout_manifest

REPO = Path(__file__).resolve().parents[2]
HELDOUT = REPO / "evals" / "heldout"
SCHEMA_NAME = "heldout-manifest.schema.json"


def test_git_ls_files_lists_only_heldout_manifest() -> None:
    if shutil.which("git") is None or not (REPO / ".git").exists():
        pytest.skip("needs a git checkout")
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "evals/heldout"],
        cwd=REPO,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8").split("\0")
    paths = [p for p in listed if p]
    assert f"evals/heldout/{SCHEMA_NAME}" in paths
    for path in paths:
        name = path.removeprefix("evals/heldout/")
        assert "/" not in name, f"nested path under evals/heldout: {path}"
        if name == SCHEMA_NAME:
            continue
        assert name.endswith(".manifest.json"), f"unexpected file under evals/heldout: {path}"
        load_heldout_manifest(REPO / path)  # strict: no per-file rows can sneak in


def _synthetic_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    public = root / "evals" / "golden" / "repo-chore" / "public-fixture"
    hidden = root / "evals" / "heldout" / "repo-chore" / "h001"
    for path in (public, hidden):
        path.mkdir(parents=True)
        (path / "task.json").write_text('{"expected_skill_id": "public-fixture"}\n')
        (path / "goal.json").write_text("{}\n")
    return root


def test_golden_discovery_never_reaches_evals_heldout(tmp_path: Path) -> None:
    root = _synthetic_repo(tmp_path)
    golden_root = root / "evals" / "golden"
    assert discover_golden(golden_root, "h001", "repo-chore") is None
    assert discover_golden(golden_root, "public-fixture", "repo-chore") is not None
    assert [p.name for p in list_goldens_for_task_class(golden_root, "repo-chore")] == [
        "public-fixture"
    ]
    assert sorted(golden_root.glob("*/*/goal.json")) == [
        golden_root / "repo-chore" / "public-fixture" / "goal.json"
    ]


def test_repo_golden_discovery_and_plan_glob_skip_evals_heldout() -> None:
    from tests.plan.test_plan_acceptance import GOALS

    heldout = HELDOUT.resolve()
    golden_root = REPO / "evals" / "golden"
    found: list[Path] = list(GOALS)
    for class_dir in golden_root.iterdir():
        if class_dir.is_dir():
            found.extend(list_goldens_for_task_class(golden_root, class_dir.name))
    for path in found:
        assert heldout not in path.resolve().parents
