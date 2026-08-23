"""MEA multiphase golden under the eval/lift harness.

Skill-free Goal fixture at evals/golden/mea/synthetic-multiphase/.
Not on the repo-chore promotion gate. Eval-firewall keeps rows out of lift.
"""

from __future__ import annotations

from pathlib import Path

from recertia.evals.golden import list_goldens_for_task_class, run_eval_suite, run_goal_fixture
from recertia.evals.store import EvalStore
from recertia.mea.store import AuditedStateStore
from recertia.trajectory.store import TrajectoryStore

REPO = Path(__file__).resolve().parents[2]
GOLDEN_ROOT = REPO / "evals" / "golden"
MEA_GOLDEN = GOLDEN_ROOT / "mea" / "synthetic-multiphase"


def test_mea_fixture_is_not_on_repo_chore_promotion_gate() -> None:
    repo_chore = {p.name for p in list_goldens_for_task_class(GOLDEN_ROOT, "repo-chore")}
    mea = {p.name for p in list_goldens_for_task_class(GOLDEN_ROOT, "mea")}
    assert "synthetic-multiphase" not in repo_chore
    assert "synthetic-multiphase" in mea


def test_eval_suite_mea_writes_sidecar_and_is_firewalled_from_lift(tmp_path: Path) -> None:
    assert MEA_GOLDEN.is_dir()
    eval_db = tmp_path / "evals.db"
    runs_root = tmp_path / "runs"
    store = EvalStore(eval_db)
    try:
        report = run_eval_suite(
            task_class="mea",
            golden_root=GOLDEN_ROOT,
            skills_root=tmp_path / "no-skills",
            runs_root=runs_root,
            eval_store=store,
            snapshot_id="mea-lift-test",
        )
        counts = store.arm_counts(task_class="mea", snapshot_id="mea-lift-test")
    finally:
        store.close()

    assert report.results, "skill-free mea goldens must still run"
    assert report.all_passed
    result = report.results[0]
    assert result.terminal == "solved"
    assert result.run_id

    sidecar = AuditedStateStore(runs_root / "golden-runs" / "audited_states").load(result.run_id)
    assert sidecar is not None
    assert sidecar.version >= 1
    assert sidecar.current_phase == "complete"
    assert {d.decision_id for d in sidecar.verified_decisions} == {
        "phase1_file",
        "phase2_file",
    }

    events = TrajectoryStore(runs_root / "golden-runs" / "trajectories").list_events(
        result.run_id
    )
    assert any(e.event_kind == "audited_state_delta" for e in events)

    # Eval firewall: fixture rows exist but cannot enter causal_lift samples.
    assert not counts


def test_run_goal_fixture_direct(tmp_path: Path) -> None:
    result = run_goal_fixture(MEA_GOLDEN, runs_root=tmp_path / "runs")
    assert result.passed
    assert result.terminal == "solved"
    assert "mea_active=True" in result.detail
