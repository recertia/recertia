"""Follow-up regressions for remaining medium code-review findings."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.budget import Budget, BudgetReservation, Spend
from contracts.criteria import SensitivityProof, SkillCertificationCriterion
from contracts.skill import Hygiene, Provenance, SkillVersion, Step
from contracts.status import SkillStatus
from recertia.memory.procedural.store import LifecycleConflictError, SkillStore
from recertia.review.shadow import enter_shadow


def _version(skill_id: str) -> SkillVersion:
    proof = SensitivityProof(
        criterion_id="ok",
        negative_fixture="empty",
        rejected=True,
        checked_at=datetime.now(timezone.utc),
        evidence_hash="abc",
    )
    return SkillVersion(
        skill_id=skill_id,
        version=1,
        title=f"Title for {skill_id} skill",
        intent=f"Intent text long enough for {skill_id} skill version contract.",
        task_class="repo-chore",
        steps=[
            Step(
                id="step_1",
                tool="shell",
                intent="Run a trivial shell step for the follow-up fixture",
                inputs={"command": "true"},
            )
        ],
        certification_criteria=[
            SkillCertificationCriterion(
                id="ok",
                kind="command",
                run="true",
                sensitivity_proof=proof,
                preregistered=True,
            )
        ],
        provenance=Provenance(
            distilled_from_run="review",
            distilled_at=datetime.now(timezone.utc),
            curation="human_authored",
            authoring_prior_version="ap-test",
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=datetime.now(timezone.utc)),
    )


def test_write_status_cas_rejects_stale_lifecycle(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    version = _version("cas-demo")
    store.write_version(version)
    store.write_status(SkillStatus(skill_id="cas-demo", version=1, lifecycle="candidate"))
    with pytest.raises(LifecycleConflictError, match="expected lifecycle"):
        store.write_status(
            SkillStatus(skill_id="cas-demo", version=1, lifecycle="quarantined", active=False),
            expected_lifecycle="approved",
        )


def test_enter_shadow_refuses_quarantined(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    version = _version("q-demo")
    store.write_version(version)
    store.write_status(SkillStatus(skill_id="q-demo", version=1, lifecycle="quarantined"))
    with pytest.raises(ValueError, match="enter_shadow refused"):
        enter_shadow(store, "q-demo", 1)


def test_enter_shadow_from_candidate(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    version = _version("c-demo")
    store.write_version(version)
    store.write_status(SkillStatus(skill_id="c-demo", version=1, lifecycle="candidate"))
    enter_shadow(store, "c-demo", 1)
    assert store.get_status("c-demo", 1).lifecycle == "shadow"


def test_branch_budget_preflight_marks_timed_out(tmp_path: Path) -> None:
    from contracts.branch import BranchState
    from contracts.run import RunManifest, RunState, Task
    from recertia.graph.ops import OperationLedger
    from recertia.ledger import HashChainLedger
    from recertia.nodes.context import NodeContext
    from recertia.nodes.solve import solve
    from recertia.workspace import WorkspaceManager

    work = tmp_path / "work"
    work.mkdir()
    state = RunState(
        run_id="budget-branch",
        task=Task(
            task_id="t",
            request="branch budget",
            task_class="repo-chore",
            submitted_at=datetime.now(timezone.utc),
        ),
        manifest=RunManifest(),
        strategy="portfolio",
        budget=Budget(max_tool_calls=100),
        branches=[
            BranchState(
                branch_id="budget-branch-p0",
                kind="portfolio",
                strategy="scratch",
                workspace_ref=str(work / "budget-branch-p0"),
                budget=Budget(max_tool_calls=1),
                reserved=BudgetReservation(tool_calls=1),
                status="dispatched",
                spent=Spend(tool_calls=1),
            )
        ],
    )
    with closing(OperationLedger(tmp_path / "ops.db")) as ops:
        ctx = NodeContext(
            run_id="budget-branch",
            attempt_no=0,
            node="solve",
            workdir=work,
            workspaces=WorkspaceManager(tmp_path / "snapshots"),
            ledger=HashChainLedger(tmp_path / "ledger.jsonl"),
            ops=ops,
            script=["true", "true"],
        )
        outcome = solve(state, ctx)
        assert outcome.state.branches[0].status == "timed_out"
