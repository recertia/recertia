"""MEA runtime bind / auditor CAS / sidecar store tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from contracts.criteria import CriterionResult, TaskCriterion
from contracts.goal import DesiredState, Goal
from contracts.policy import ImprovementFlags, Policy
from contracts.run import RunManifest, RunState, Task
from recertia.ledger import HashChainLedger
from recertia.mea.runtime import (
    apply_validate_audit,
    audit_after_validate,
    auditor_conversation_id,
    bind_after_intake,
    create_audited_state,
    executor_conversation_id,
    resolve_from_run,
)
from recertia.mea.store import AuditedStateStore
from recertia.nodes._util import criteria_hash
from recertia.trajectory.emitter import TrajectoryEmitter


def _crit(cid: str, run: str = "true") -> TaskCriterion:
    return TaskCriterion(
        id=cid,
        kind="command",
        run=run,
        expect_exit=0,
        weight=1.0,
        source="caller",
        preregistered=True,
    )


def _goal(*, opt_in: bool = False) -> Goal:
    return Goal(
        goal_id="g_synthetic_multiphase",
        desired=[
            DesiredState(id="phase1_file", kind="file_exists", path="phase1.done"),
            DesiredState(id="phase2_file", kind="file_exists", path="phase2.done"),
        ],
        context="Synthetic multi-phase Goal for MEA lift-harness readiness",
        mea_opt_in=opt_in,
    )


def _policy(*, enabled: bool = False) -> Policy:
    return Policy(
        version="test",
        authoring_prior_version="ap-test",
        improvement=ImprovementFlags(mea_enabled=enabled),
    )


def _state(
    *,
    criteria: list[TaskCriterion] | None = None,
    opt_in: bool = False,
    strategy: str = "single",
    mea_active: bool = False,
) -> RunState:
    criteria = criteria or [_crit("phase1_file"), _crit("phase2_file")]
    return RunState(
        run_id="run-mea",
        task=Task(
            task_id="t-mea",
            goal=_goal(opt_in=opt_in),
            execution_strategy=strategy,  # type: ignore[arg-type]
            submitted_at=datetime.now(timezone.utc),
            is_eval_fixture=True,
        ),
        criteria=criteria,
        manifest=RunManifest(criteria_hash=criteria_hash(criteria)),
        mea_active=mea_active,
    )


def test_resolve_from_run_default_off():
    a = resolve_from_run(_state(), None)
    assert a.active is False
    assert a.fallback_reason == "policy_mea_disabled"


def test_resolve_from_run_all_three_layers():
    a = resolve_from_run(_state(opt_in=True, strategy="mea"), _policy(enabled=True))
    assert a.active is True


def test_bind_after_intake_default_off_is_identity(tmp_path: Path):
    state = _state()
    store = AuditedStateStore(tmp_path / "audited_states")
    ledger = HashChainLedger(tmp_path / "ledger.jsonl")
    out = bind_after_intake(state, policy=None, store=store, ledger=ledger)
    assert out is state
    assert not (tmp_path / "audited_states").exists()
    assert ledger.entries() == []


def test_bind_after_intake_creates_sidecar_when_active(tmp_path: Path):
    state = _state(opt_in=True, strategy="mea")
    store = AuditedStateStore(tmp_path / "audited_states")
    ledger = HashChainLedger(tmp_path / "ledger.jsonl")
    out = bind_after_intake(state, policy=_policy(enabled=True), store=store, ledger=ledger)
    assert out.mea_active is True
    assert out.mea_fallback_reason is None
    loaded = store.load(state.run_id)
    assert loaded is not None
    assert loaded.version == 0
    assert loaded.current_phase == "intake"
    assert [c.id for c in loaded.acceptance_criteria] == ["phase1_file", "phase2_file"]
    assert ledger.entries() == []


def test_bind_after_intake_notes_incomplete_activation(tmp_path: Path):
    state = _state(opt_in=True, strategy="mea")
    store = AuditedStateStore(tmp_path / "audited_states")
    ledger = HashChainLedger(tmp_path / "ledger.jsonl")
    out = bind_after_intake(state, policy=None, store=store, ledger=ledger)
    assert out.mea_active is False
    assert out.mea_fallback_reason == "policy_mea_disabled"
    assert store.load(state.run_id) is None
    entries = ledger.entries()
    assert len(entries) == 1
    assert entries[0].action == "mea_activation_fallback"
    assert entries[0].evidence["reason"] == "policy_mea_disabled"


def test_auditor_conversation_is_fresh():
    assert executor_conversation_id("r1", 1) != auditor_conversation_id("r1", 1)


def test_validate_audit_cas_and_trajectory_payload(tmp_path: Path):
    state = _state(opt_in=True, strategy="mea", mea_active=True)
    policy = _policy(enabled=True)
    store = AuditedStateStore(tmp_path / "audited_states")
    bind_after_intake(state, policy=policy, store=store, ledger=None)
    scored = state.model_copy(
        update={
            "results": [
                CriterionResult(criterion_id="phase1_file", kind="command", passed=True),
                CriterionResult(criterion_id="phase2_file", kind="command", passed=True),
            ]
        }
    )
    out, delta = audit_after_validate(scored, store=store, attempt_no=1)
    assert out.mea_active is True
    assert delta is not None
    assert delta.parent_version == 0
    assert delta.proposed_version == 1
    assert delta.current_phase == "complete"
    loaded = store.load(state.run_id)
    assert loaded is not None
    assert loaded.version == 1
    assert {d.decision_id for d in loaded.verified_decisions} == {
        "phase1_file",
        "phase2_file",
    }
    event = TrajectoryEmitter().from_auditor_delta(
        out, node="validate", attempt_no=1, delta=delta
    )
    assert event.event_kind == "audited_state_delta"
    assert event.payload_inline is not None
    assert event.payload_inline["proposed_version"] == 1


def test_validate_audit_no_op_when_inactive(tmp_path: Path):
    state = _state()
    store = AuditedStateStore(tmp_path / "audited_states")
    out, delta = audit_after_validate(state, store=store, attempt_no=1)
    assert out is state
    assert delta is None


def test_create_audited_state_uses_locked_hash():
    state = _state()
    ats = create_audited_state(state, policy=_policy(enabled=True))
    assert ats.criteria_snapshot_hash == state.manifest.criteria_hash
    assert ats.provenance.source == "mea_loop"


def test_apply_validate_audit_rejects_stale_parent():
    state = _state(mea_active=True)
    ats = create_audited_state(state, policy=_policy(enabled=True))
    scored = state.model_copy(
        update={
            "results": [
                CriterionResult(criterion_id="phase1_file", kind="command", passed=True),
                CriterionResult(criterion_id="phase2_file", kind="command", passed=True),
            ]
        }
    )
    first, delta, err = apply_validate_audit(ats, scored, attempt_no=1)
    assert err is None and first is not None and delta is not None
    again, delta2, err2 = apply_validate_audit(ats, scored, attempt_no=1)
    # Applying the same parent twice: second CAS against stale v0 copy succeeds as a
    # new propose (parent still 0 on the passed-in copy). Engine always loads the
    # sidecar tip, so this documents the helper is not itself a lock.
    assert again is not None and delta2 is not None and err2 is None
    # Against the advanced tip, parent mismatch:
    stale_delta = delta.model_copy(update={"parent_version": 0, "proposed_version": 1})
    from contracts.audited_task_state import apply_auditor_delta

    rejected, reason = apply_auditor_delta(first, stale_delta)
    assert rejected is None
    assert reason is not None and reason.startswith("cas_mismatch")
