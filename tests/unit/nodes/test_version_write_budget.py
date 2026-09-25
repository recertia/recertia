"""ADR-0017: the version-write cap is a real budget dimension."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from contracts.budget import Budget, BudgetReservation, Spend, budget_excess
from contracts.criteria import SensitivityProof, SkillCertificationCriterion
from contracts.run import ReusabilityVerdict, RunState
from contracts.skill import Hygiene, Provenance, SkillVersion, Step
from recertia.memory.procedural.store import SkillStore
from recertia.nodes.context import NodeContext
from recertia.nodes.distill import distill
from recertia.nodes.review import review
from recertia.nodes.store import store


def _draft() -> SkillVersion:
    now = datetime.now(timezone.utc)
    proof = SensitivityProof(
        criterion_id="ok",
        negative_fixture="empty",
        rejected=True,
        checked_at=now,
    )
    return SkillVersion(
        skill_id="unit-demo-skill",
        version=1,
        title="Unit demo skill title",
        intent="Intent long enough for the skill version contract minimum.",
        task_class="repo-chore",
        steps=[
            Step(
                id="step_1",
                tool="shell",
                intent="Write a trivial marker file into the workspace",
                inputs={"command": "echo hi > output.txt"},
            )
        ],
        certification_criteria=[
            SkillCertificationCriterion(
                id="ok",
                kind="command",
                run="test -f output.txt",
                sensitivity_proof=proof,
                preregistered=True,
            )
        ],
        provenance=Provenance(
            distilled_from_run="unit-run",
            distilled_at=now,
            curation="self_distilled",
            authoring_prior_version="ap-test",
        ),
        hygiene=Hygiene(secret_scan="skipped"),
    )


def test_budget_excess_counts_versions_written() -> None:
    budget = Budget(max_versions_written=2)
    spent = Spend(versions_written=2)
    assert (
        budget_excess(budget, spent, BudgetReservation(), BudgetReservation(versions_written=1))
        == "versions_written"
    )
    assert budget_excess(budget, Spend(versions_written=1), BudgetReservation(), BudgetReservation()) is None


def test_zero_cap_admits_no_writes() -> None:
    assert (
        budget_excess(
            Budget(max_versions_written=0),
            Spend(),
            BudgetReservation(),
            BudgetReservation(versions_written=1),
        )
        == "versions_written"
    )


def test_store_charges_versions_written(
    base_state: RunState, ctx: NodeContext, tmp_path
) -> None:
    ctx.store = SkillStore(tmp_path / "skills")
    state = base_state.model_copy(update={"draft": _draft().model_dump(mode="json")})
    outcome = store(state, ctx)
    assert outcome.state.spent.versions_written == 1
    assert len(outcome.state.written_versions) == 1


def test_store_refuses_when_cap_is_spent(
    base_state: RunState, ctx: NodeContext, tmp_path
) -> None:
    ctx.store = SkillStore(tmp_path / "skills")
    state = base_state.model_copy(
        update={
            "draft": _draft().model_dump(mode="json"),
            "budget": Budget(max_versions_written=0),
        }
    )
    with pytest.raises(ValueError, match="versions_written budget exhausted"):
        store(state, ctx)
    assert list((tmp_path / "skills").glob("*")) == []


def test_distill_is_one_off_when_write_budget_is_spent(
    base_state: RunState, ctx: NodeContext
) -> None:
    state = base_state.model_copy(
        update={
            "budget": Budget(max_versions_written=0),
            "strategy": "scratch",
            "reusability": None,
        }
    )
    outcome = distill(state, ctx)
    assert outcome.route == "one_off"
    assert outcome.state.draft is None
    assert outcome.state.reusability is not None
    assert "version write budget" in (outcome.state.reusability.reason or "")


def test_review_rejects_approval_when_write_budget_is_spent(
    base_state: RunState, ctx: NodeContext
) -> None:
    class _Approver:
        def enqueue(self, version, *, run_id):
            return None

        def decide(self, version, *, run_id, reviewer):
            return type("D", (), {"outcome": "approved", "note": "ok"})()

    ctx.reviewer = _Approver()
    state = base_state.model_copy(
        update={
            "draft": _draft().model_dump(mode="json"),
            "budget": Budget(max_versions_written=1),
            "spent": Spend(versions_written=1),
        }
    )
    outcome = review(state, ctx)
    assert outcome.route == "rejected"
    assert "version write budget" in (outcome.note or "")


def test_distill_does_not_block_existing_skill_evidence(
    base_state: RunState, ctx: NodeContext
) -> None:
    from contracts.run import SkillCandidateRef

    state = base_state.model_copy(
        update={
            "budget": Budget(max_versions_written=0),
            "strategy": "apply",
            "chosen": SkillCandidateRef(skill_id="existing", version=1, score=1.0),
            "reusability": ReusabilityVerdict(
                verdict="one_off",
                parameterisable=True,
                context_free=True,
                checkable=True,
                not_duplicate=True,
                bounded=True,
                reason="placeholder",
            ),
        }
    )
    outcome = distill(state, ctx)
    assert outcome.route == "one_off"
    assert "existing skill" in (outcome.note or "")
