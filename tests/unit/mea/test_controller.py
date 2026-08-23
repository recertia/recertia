"""MEA controller pure-helper tests."""

from __future__ import annotations

from datetime import datetime, timezone

from contracts.audited_task_state import AuditedTaskState, ProvenanceBundle
from contracts.budget import Budget
from contracts.criteria import TaskCriterion
from recertia.mea.controller import (
    enforce_round_budget,
    propose_subtask,
    require_fresh_auditor_context,
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


def _state(**kwargs) -> AuditedTaskState:
    defaults = dict(
        state_id="st_1",
        goal_id="g_1",
        version=0,
        objective="obj",
        acceptance_criteria=[_crit()],
        criteria_snapshot_hash="h",
        provenance=ProvenanceBundle(source="synthetic"),
        updated_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
        max_rounds=3,
        rounds_consumed=0,
        budget_residual=Budget(max_attempts=4),
    )
    defaults.update(kwargs)
    return AuditedTaskState(**defaults)


def test_enforce_max_rounds():
    st = _state(rounds_consumed=3, max_rounds=3)
    stop = enforce_round_budget(st)
    assert stop is not None and stop.reason == "max_rounds_reached"


def test_enforce_residual_attempts():
    st = _state(budget_residual=Budget(max_attempts=0))
    stop = enforce_round_budget(st)
    assert stop is not None and stop.reason == "residual_attempts_exhausted"


def test_propose_subtask_ok():
    st = _state()
    contract, stop = propose_subtask(st, subtask_id="sub_1", description="do work")
    assert stop is None and contract is not None
    assert contract.parent_state_version == 0
    assert contract.budget.max_attempts >= 1


def test_auditor_must_be_fresh():
    reason = require_fresh_auditor_context(
        executor_conversation_id="c1",
        auditor_conversation_id="c1",
        executor_model_ref="m1",
        auditor_model_ref="m1",
    )
    assert reason == "auditor_shares_executor_conversation"


def test_auditor_ok_when_distinct():
    reason = require_fresh_auditor_context(
        executor_conversation_id="c1",
        auditor_conversation_id="c2",
        executor_model_ref="m1",
        auditor_model_ref="m2",
    )
    assert reason is None
