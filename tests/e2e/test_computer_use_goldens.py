"""Skill-free computer-use goldens under the eval/lift harness.

Fixtures live at evals/golden/{bug_reproduction,playtest_operator,docs_auditor}/.
Not on the repo-chore promotion gate. Eval-firewall keeps rows out of lift.
"""

from __future__ import annotations

from pathlib import Path

from contracts.computer_use_goldens import GOLDEN_TASK_CLASSES
from recertia.evals.golden import list_goldens_for_task_class, run_eval_suite, run_goal_fixture
from recertia.evals.store import EvalStore

REPO = Path(__file__).resolve().parents[2]
GOLDEN_ROOT = REPO / "evals" / "golden"


def test_computer_use_fixtures_are_not_on_repo_chore_promotion_gate() -> None:
    repo_chore = {p.name for p in list_goldens_for_task_class(GOLDEN_ROOT, "repo-chore")}
    for task_class, descriptor in GOLDEN_TASK_CLASSES.items():
        found = list_goldens_for_task_class(GOLDEN_ROOT, task_class)
        assert found, f"missing goldens for {task_class}"
        names = {p.name for p in found}
        assert names.isdisjoint(repo_chore)
        for golden in found:
            assert (golden / "goal.json").is_file()
            assert (golden / "workspace").is_dir()
            for artifact in descriptor.required_artifacts:
                # Required artifacts are represented as files in the workspace
                # (screenshot/HAR/log/diff/list). Directory-shaped names map to a file.
                assert any(golden.joinpath("workspace").rglob("*")), artifact


def test_eval_suite_computer_use_is_firewalled_from_lift(tmp_path: Path) -> None:
    eval_db = tmp_path / "evals.db"
    runs_root = tmp_path / "runs"
    store = EvalStore(eval_db)
    try:
        for task_class in GOLDEN_TASK_CLASSES:
            report = run_eval_suite(
                task_class=task_class,
                golden_root=GOLDEN_ROOT,
                skills_root=tmp_path / "no-skills",
                runs_root=runs_root,
                eval_store=store,
                snapshot_id="cu-lift-test",
            )
            counts = store.arm_counts(task_class=task_class, snapshot_id="cu-lift-test")
            assert report.results, f"skill-free {task_class} goldens must still run"
            assert report.all_passed, [r.detail for r in report.results]
            assert report.results[0].terminal == "solved"
            assert not counts
    finally:
        store.close()


def test_run_goal_fixture_direct(tmp_path: Path) -> None:
    golden = GOLDEN_ROOT / "bug_reproduction" / "synthetic-repro"
    result = run_goal_fixture(golden, runs_root=tmp_path / "runs")
    assert result.passed
    assert result.terminal == "solved"
    assert "mea_active=False" in result.detail
