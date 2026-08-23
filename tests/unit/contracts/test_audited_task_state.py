"""Phase 0 unit tests for AuditedTaskState and MEA contracts."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from contracts.audited_task_state import (
    ArtifactRef,
    AuditedTaskState,
    AuditorDelta,
    Blocker,
    ProvenanceBundle,
    SubtaskContract,
    VerifiedDecision,
    apply_auditor_delta,
)
from contracts.budget import Budget, ResidualBudget
from contracts.criteria import TaskCriterion


def _criterion(cid: str = "c1") -> TaskCriterion:
    return TaskCriterion(
        id=cid,
        kind="command",
        run="true",
        expect_exit=0,
        weight=1.0,
        source="caller",
        preregistered=True,
    )


def _provenance() -> ProvenanceBundle:
    return ProvenanceBundle(source="synthetic", source_ref="test")


def _base_state(**kwargs) -> AuditedTaskState:
    defaults = {
        "state_id": "st_1",
        "goal_id": "g_1",
        "version": 0,
        "objective": "test objective",
        "acceptance_criteria": [_criterion()],
        "criteria_snapshot_hash": "abc123",
        "provenance": _provenance(),
        "updated_at": datetime(2026, 8, 23, tzinfo=timezone.utc),
        "max_rounds": 3,
    }
    defaults.update(kwargs)
    return AuditedTaskState(**defaults)


def _delta(parent: int = 0, **kwargs) -> AuditorDelta:
    defaults = {
        "report_id": "ar_1",
        "parent_version": parent,
        "proposed_version": parent + 1,
        "criteria_snapshot_hash": "abc123",
        "isolation_policy_ref": "container_default",
        "budget_residual": ResidualBudget(),
        "produced_at": datetime(2026, 8, 23, 12, tzinfo=timezone.utc),
    }
    defaults.update(kwargs)
    return AuditorDelta(**defaults)


class TestAuditedTaskStateCAS:
    def test_cas_success_advances_version_and_rounds(self):
        state = _base_state()
        decision = VerifiedDecision(
            decision_id="d1",
            statement="file exists",
            evidence_refs=[ArtifactRef(kind="file", ref="/tmp/x")],
            verified_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
        )
        delta = _delta(verified_decisions_added=[decision], current_phase="execute")
        new_state, err = apply_auditor_delta(state, delta)
        assert err is None
        assert new_state is not None
        assert new_state.version == 1
        assert new_state.rounds_consumed == 1
        assert new_state.current_phase == "execute"
        assert new_state.last_auditor_report_id == "ar_1"
        assert len(new_state.verified_decisions) == 1

    def test_cas_rejects_version_mismatch(self):
        state = _base_state(version=2)
        delta = _delta(parent=0)
        new_state, err = apply_auditor_delta(state, delta)
        assert new_state is None
        assert err is not None and "cas_mismatch" in err

    def test_cas_rejects_criteria_hash_drift(self):
        state = _base_state()
        delta = _delta(criteria_snapshot_hash="different")
        new_state, err = apply_auditor_delta(state, delta)
        assert new_state is None
        assert err == "criteria_snapshot_hash_drift"

    def test_cas_rejects_isolation_mismatch(self):
        state = _base_state(isolation_policy_ref="container_default")
        delta = _delta(isolation_policy_ref="external_computer")
        new_state, err = apply_auditor_delta(state, delta)
        assert new_state is None
        assert err is not None and "isolation_policy_ref_mismatch" in err

    def test_cas_rejects_max_rounds_exceeded(self):
        state = _base_state(rounds_consumed=3, max_rounds=3)
        delta = _delta()
        new_state, err = apply_auditor_delta(state, delta)
        assert new_state is None
        assert err is not None and "max_rounds_exceeded" in err

    def test_cas_clears_and_sets_blockers(self):
        state = _base_state(
            current_blockers=[
                Blocker(blocker_id="b1", description="old", since_version=0)
            ]
        )
        delta = _delta(
            blockers_cleared=["b1"],
            blockers_set=[
                Blocker(blocker_id="b2", description="new", since_version=1)
            ],
        )
        new_state, err = apply_auditor_delta(state, delta)
        assert err is None and new_state is not None
        assert [b.blocker_id for b in new_state.current_blockers] == ["b2"]


class TestAuditorDeltaValidation:
    def test_proposed_must_be_parent_plus_one(self):
        with pytest.raises(ValidationError):
            _delta(parent=0, proposed_version=2)


class TestVerifiedDecision:
    def test_requires_evidence(self):
        with pytest.raises(ValidationError):
            VerifiedDecision(
                decision_id="d1",
                statement="claim",
                evidence_refs=[],
                verified_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
            )


class TestRoundTrip:
    def test_model_dump_roundtrip(self):
        state = _base_state()
        restored = AuditedTaskState.model_validate(state.model_dump(mode="json"))
        assert restored.state_id == state.state_id
        assert restored.version == state.version
        assert restored.criteria_snapshot_hash == state.criteria_snapshot_hash


class TestSubtaskContract:
    def test_minimal_subtask(self):
        st = SubtaskContract(
            subtask_id="st_1",
            parent_state_version=0,
            description="do the thing",
            acceptance_criteria=[_criterion()],
        )
        assert st.max_steps == 50
        assert st.budget.max_attempts == 4


class TestResidualBudgetIsolation:
    def test_zero_attempts_allowed_on_residual(self):
        r = ResidualBudget(max_attempts=0)
        assert r.max_attempts == 0

    def test_goal_budget_rejects_zero_attempts(self):
        with pytest.raises(ValidationError):
            Budget(max_attempts=0)

    def test_as_admission_budget_clamps_zeros(self):
        r = ResidualBudget(max_attempts=0, max_tool_calls=0, max_wall_clock_s=0)
        b = r.as_admission_budget()
        assert b.max_attempts >= 1
        assert b.max_tool_calls >= 1
        assert b.max_wall_clock_s >= 1
