"""Phase 2 unit tests for TrajectoryImport under MEA rules."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from contracts.audited_task_state import ProvenanceBundle
from contracts.criteria import TaskCriterion
from contracts.trajectory_import import (
    EnvironmentDescriptor,
    TrajectoryImport,
    TrajectoryStep,
    import_may_attach_mea,
    import_may_promote,
)


def _crit() -> TaskCriterion:
    return TaskCriterion(
        id="c1",
        kind="command",
        run="true",
        expect_exit=0,
        weight=1.0,
        source="caller",
        preregistered=True,
    )


def _step() -> TrajectoryStep:
    return TrajectoryStep(seq=0, action="click", target="#btn")


def _imp(**kwargs) -> TrajectoryImport:
    defaults = {
        "import_id": "imp_1",
        "source": "synthetic",
        "source_ref": "fixture/1",
        "captured_at": datetime(2026, 8, 23, tzinfo=timezone.utc),
        "environment": EnvironmentDescriptor(os="linux", tools=["browser"]),
        "steps": [_step()],
        "outcome": "solved",
        "criteria_snapshot": [_crit()],
        "provenance": ProvenanceBundle(source="synthetic", source_ref="fixture/1"),
        "reexecutable": True,
        "require_auditor_reverify": True,
        "mea_goal_id": "g_1",
    }
    defaults.update(kwargs)
    return TrajectoryImport(**defaults)


class TestTrajectoryImportValidation:
    def test_rejects_empty_environment(self):
        with pytest.raises(ValidationError):
            _imp(environment=EnvironmentDescriptor())

    def test_rejects_empty_source_ref(self):
        with pytest.raises(ValidationError):
            _imp(source_ref="  ")


class TestPromotionAndMeaGates:
    def test_promote_ok_when_reexecutable_and_reverify(self):
        ok, reason = import_may_promote(_imp())
        assert ok and reason == "ok"

    def test_promote_blocked_when_not_reexecutable(self):
        ok, reason = import_may_promote(_imp(reexecutable=False))
        assert not ok and reason == "not_reexecutable"

    def test_promote_blocked_when_reverify_disabled(self):
        ok, reason = import_may_promote(_imp(require_auditor_reverify=False))
        assert not ok and reason == "auditor_reverify_disabled"

    def test_mea_attach_requires_goal_id(self):
        ok, reason = import_may_attach_mea(_imp(mea_goal_id=None))
        assert not ok and reason == "missing_mea_goal_id"

    def test_mea_attach_ok(self):
        ok, reason = import_may_attach_mea(_imp())
        assert ok and reason == "ok"
