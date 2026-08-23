"""Phase 3 unit tests for MEA Systems brief and stuck detection."""

from __future__ import annotations

from datetime import datetime, timezone

from contracts.audited_task_state import (
    AuditedTaskState,
    Blocker,
    ProvenanceBundle,
    VerifiedDecision,
    ArtifactRef,
)
from contracts.criteria import TaskCriterion
from recertia.ops.mea_systems import (
    build_mea_systems_brief,
    detect_stuck,
    evidence_coverage,
)


def _crit(cid: str = "c1") -> TaskCriterion:
    return TaskCriterion(
        id=cid,
        kind="command",
        run="true",
        expect_exit=0,
        weight=1.0,
        source="caller",
        preregistered=True,
    )


def _state(**kwargs) -> AuditedTaskState:
    defaults = dict(
        state_id="st_1",
        goal_id="g_1",
        version=1,
        objective="obj",
        acceptance_criteria=[_crit("c1"), _crit("c2")],
        criteria_snapshot_hash="hash",
        provenance=ProvenanceBundle(source="synthetic"),
        updated_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
        max_rounds=12,
        rounds_consumed=1,
    )
    defaults.update(kwargs)
    return AuditedTaskState(**defaults)


class TestEvidenceCoverage:
    def test_zero_when_no_decisions(self):
        assert evidence_coverage(_state()) == 0.0

    def test_ratio(self):
        decision = VerifiedDecision(
            decision_id="d1",
            statement="ok",
            evidence_refs=[ArtifactRef(kind="file", ref="/x")],
            verified_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
        )
        st = _state(verified_decisions=[decision])
        assert evidence_coverage(st) == 0.5


class TestStuckDetection:
    def test_persistent_blocker(self):
        prev = _state(
            version=0,
            current_blockers=[Blocker(blocker_id="b1", description="x", since_version=0)],
        )
        curr = _state(
            version=1,
            current_blockers=[Blocker(blocker_id="b1", description="x", since_version=0)],
        )
        stuck, reason = detect_stuck(curr, previous=prev)
        assert stuck and reason is not None and reason.startswith("persistent_blocker")

    def test_no_progress_near_floor(self):
        prev = _state(version=0, rounds_consumed=10, max_rounds=12)
        curr = _state(version=1, rounds_consumed=11, max_rounds=12)
        stuck, reason = detect_stuck(curr, previous=prev, residual_rounds_floor=2)
        assert stuck and reason == "no_progress_near_round_floor"

    def test_not_stuck_when_progress(self):
        decision = VerifiedDecision(
            decision_id="d1",
            statement="ok",
            evidence_refs=[ArtifactRef(kind="file", ref="/x")],
            verified_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
        )
        prev = _state(version=0, rounds_consumed=10, max_rounds=12)
        curr = _state(
            version=1,
            rounds_consumed=11,
            max_rounds=12,
            verified_decisions=[decision],
        )
        stuck, reason = detect_stuck(curr, previous=prev, residual_rounds_floor=2)
        assert not stuck and reason is None


class TestSystemsBrief:
    def test_brief_fields(self):
        brief = build_mea_systems_brief(_state())
        assert brief.goal_id == "g_1"
        assert brief.rounds_remaining == 11
        assert brief.evidence_coverage == 0.0
        assert brief.stuck is False
