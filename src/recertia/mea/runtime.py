"""Engine-side MEA binding. No new graph nodes.

Intake (after the existing intake node) creates AuditedTaskState when all three
activation layers are true. Validate (after the existing validate node) acts as
a fresh-context auditor and compare-and-swaps an AuditorDelta. Fallback when
MEA was requested but incomplete is a ledger note — helpers cannot emit it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from contracts.audited_task_state import (
    ArtifactRef,
    AuditedTaskState,
    AuditorDelta,
    Blocker,
    ProvenanceBundle,
    VerifiedDecision,
    apply_auditor_delta,
)
from contracts.budget import ResidualBudget
from contracts.policy import Policy
from contracts.run import RunState
from recertia.mea.activation import MeaActivation, resolve_mea_activation, should_note_fallback
from recertia.mea.controller import enforce_round_budget, require_fresh_auditor_context
from recertia.nodes._util import criteria_hash

if TYPE_CHECKING:
    from recertia.ledger import HashChainLedger
    from recertia.mea.store import AuditedStateStore


def resolve_from_run(state: RunState, policy: Policy | None) -> MeaActivation:
    goal = state.task.goal
    return resolve_mea_activation(
        policy_mea_enabled=bool(policy is not None and policy.improvement.mea_enabled),
        goal_mea_opt_in=bool(goal is not None and goal.mea_opt_in),
        runtime_strategy=state.task.execution_strategy,
    )


def create_audited_state(
    state: RunState,
    *,
    policy: Policy | None,
    at: datetime | None = None,
) -> AuditedTaskState:
    """Controller-owned v0 projection from locked intake criteria."""

    now = at or datetime.now(timezone.utc)
    goal = state.task.goal
    goal_id = (goal.goal_id if goal is not None and goal.goal_id else None) or state.task.task_id
    objective = (
        (goal.context if goal is not None and goal.context else None)
        or state.task.request
        or state.task.task_id
    )
    max_rounds = policy.mea_max_rounds if policy is not None else 12
    residual = ResidualBudget.model_validate(state.budget.model_dump())
    snap = state.manifest.criteria_hash or criteria_hash(list(state.criteria))
    return AuditedTaskState(
        state_id=f"ats_{state.run_id}",
        goal_id=goal_id,
        version=0,
        objective=objective,
        acceptance_criteria=list(state.criteria),
        criteria_snapshot_hash=snap,
        budget_residual=residual,
        provenance=ProvenanceBundle(source="mea_loop", run_id=state.run_id),
        updated_at=now,
        current_phase="intake",
        max_rounds=max_rounds,
        isolation_policy_ref="container_default",
    )


def executor_conversation_id(run_id: str, attempt_no: int) -> str:
    return f"{run_id}:solve:{attempt_no}"


def auditor_conversation_id(run_id: str, attempt_no: int) -> str:
    return f"{run_id}:validate:{attempt_no}"


def bind_after_intake(
    state: RunState,
    *,
    policy: Policy | None,
    store: AuditedStateStore,
    ledger: HashChainLedger | None,
) -> RunState:
    """Create the sidecar projection or note incomplete activation. Identity if default-off."""

    activation = resolve_from_run(state, policy)
    if activation.active and state.criteria:
        store.save(state.run_id, create_audited_state(state, policy=policy))
        return state.model_copy(update={"mea_active": True, "mea_fallback_reason": None})
    if should_note_fallback(activation):
        if ledger is not None:
            ledger.append(
                actor="mea_controller",
                action="mea_activation_fallback",
                target=state.run_id,
                evidence={
                    "reason": activation.fallback_reason,
                    "policy_enabled": activation.policy_enabled,
                    "goal_opt_in": activation.goal_opt_in,
                    "runtime_strategy": activation.runtime_strategy,
                },
            )
        return state.model_copy(
            update={
                "mea_active": False,
                "mea_fallback_reason": activation.fallback_reason,
            }
        )
    return state


def propose_validate_delta(
    audited: AuditedTaskState,
    state: RunState,
    *,
    attempt_no: int,
    at: datetime | None = None,
) -> tuple[AuditorDelta | None, str | None]:
    """Build an AuditorDelta from validate results. Does not apply CAS."""

    now = at or datetime.now(timezone.utc)
    gate = require_fresh_auditor_context(
        executor_conversation_id=executor_conversation_id(state.run_id, attempt_no),
        auditor_conversation_id=auditor_conversation_id(state.run_id, attempt_no),
        executor_model_ref=None,
        auditor_model_ref=None,
    )
    if gate is not None:
        return None, gate
    stop = enforce_round_budget(audited)
    if stop is not None:
        return None, stop.reason

    known = {d.decision_id for d in audited.verified_decisions}
    added: list[VerifiedDecision] = []
    evidence_added: list[ArtifactRef] = []
    for result in state.results:
        if not result.passed or result.criterion_id in known:
            continue
        ref = ArtifactRef(
            kind="terminal_log",
            ref=f"criterion:{result.criterion_id}",
            description=(result.output_excerpt or None),
        )
        added.append(
            VerifiedDecision(
                decision_id=result.criterion_id,
                statement=f"criterion {result.criterion_id} passed under validate",
                evidence_refs=[ref],
                verified_at=now,
            )
        )
        evidence_added.append(ref)

    passing = {r.criterion_id for r in state.results if r.passed}
    failing = [r.criterion_id for r in state.results if not r.passed]
    existing_blocker_ids = {b.blocker_id for b in audited.current_blockers}
    blockers = [
        Blocker(
            blocker_id=f"crit:{cid}",
            description=f"criterion {cid} failed",
            since_version=audited.version + 1,
        )
        for cid in failing
        if f"crit:{cid}" not in existing_blocker_ids
    ]
    cleared = [
        b.blocker_id
        for b in audited.current_blockers
        if b.blocker_id.startswith("crit:") and b.blocker_id[5:] in passing
    ]
    residual = audited.budget_residual.model_copy(
        update={"max_attempts": max(0, audited.budget_residual.max_attempts - 1)}
    )
    n_verified = len(audited.verified_decisions) + len(added)
    n_crit = len(audited.acceptance_criteria)
    phase = "complete" if n_crit > 0 and n_verified >= n_crit and not failing else "execute"

    delta = AuditorDelta(
        report_id=f"ar_{state.run_id}_{audited.version + 1}",
        parent_version=audited.version,
        proposed_version=audited.version + 1,
        verified_decisions_added=added,
        blockers_set=blockers,
        blockers_cleared=cleared,
        current_phase=phase,
        criteria_snapshot_hash=audited.criteria_snapshot_hash,
        isolation_policy_ref=audited.isolation_policy_ref,
        budget_residual=residual,
        evidence_refs_added=evidence_added,
        produced_at=now,
    )
    return delta, None


def apply_validate_audit(
    audited: AuditedTaskState,
    state: RunState,
    *,
    attempt_no: int,
    at: datetime | None = None,
) -> tuple[AuditedTaskState | None, AuditorDelta | None, str | None]:
    """Fresh-auditor CAS. Returns (new_state, delta, None) or (None, None, reason)."""

    delta, reason = propose_validate_delta(audited, state, attempt_no=attempt_no, at=at)
    if delta is None:
        return None, None, reason
    new_state, cas_reason = apply_auditor_delta(audited, delta)
    if new_state is None:
        return None, delta, cas_reason
    return new_state, delta, None


def audit_after_validate(
    state: RunState,
    *,
    store: AuditedStateStore,
    attempt_no: int,
) -> tuple[RunState, AuditorDelta | None]:
    """Load sidecar, CAS auditor delta, persist. No-op when MEA is not active."""

    if not state.mea_active:
        return state, None
    audited = store.load(state.run_id)
    if audited is None:
        return state, None
    new_audited, delta, _reason = apply_validate_audit(audited, state, attempt_no=attempt_no)
    if new_audited is None or delta is None:
        return state, None
    store.save(state.run_id, new_audited)
    return state, delta


__all__ = [
    "MeaActivation",
    "apply_validate_audit",
    "audit_after_validate",
    "auditor_conversation_id",
    "bind_after_intake",
    "create_audited_state",
    "executor_conversation_id",
    "propose_validate_delta",
    "resolve_from_run",
    "should_note_fallback",
]
