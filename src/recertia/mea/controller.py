"""Controller-side pure helpers for the optional MEA loop.

Manager role = deterministic functions here (not a graph node).
Executor and Auditor reuse existing solve / validate paths with constraints
enforced by these gates before a subtask is admitted.
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.audited_task_state import (
    AuditedTaskState,
    ScopeDescriptor,
    SubtaskContract,
)
from contracts.budget import Budget, Spend, budget_excess
from contracts.budget import BudgetReservation
from contracts.criteria import TaskCriterion


@dataclass(frozen=True)
class MeaEarlyStop:
    reason: str
    rounds_consumed: int
    max_rounds: int


def enforce_round_budget(state: AuditedTaskState) -> MeaEarlyStop | None:
    """Hard controller limit: max_rounds and residual budget.

    Returns an early-stop reason when the next round must not start.
    """

    if state.rounds_consumed >= state.max_rounds:
        return MeaEarlyStop(
            reason="max_rounds_reached",
            rounds_consumed=state.rounds_consumed,
            max_rounds=state.max_rounds,
        )
    residual = state.budget_residual
    spent = Spend()  # residual already accounts for prior spend at the Goal level
    # Treat residual max_* as remaining capacity; if attempts are already 0, stop.
    if residual.max_attempts <= 0:
        return MeaEarlyStop(
            reason="residual_attempts_exhausted",
            rounds_consumed=state.rounds_consumed,
            max_rounds=state.max_rounds,
        )
    excess = budget_excess(residual, spent, BudgetReservation(), BudgetReservation(attempts=1))
    if excess is not None:
        return MeaEarlyStop(
            reason=f"residual_budget_excess:{excess}",
            rounds_consumed=state.rounds_consumed,
            max_rounds=state.max_rounds,
        )
    return None


def require_fresh_auditor_context(
    *,
    executor_conversation_id: str | None,
    auditor_conversation_id: str | None,
    executor_model_ref: str | None,
    auditor_model_ref: str | None,
) -> str | None:
    """Return a rejection reason if auditor is not independent.

    Architect review: auditor must use a fresh context and a distinct model
    instance (or at minimum a distinct conversation) from the executor.
    """

    if auditor_conversation_id is None:
        return "auditor_missing_conversation"
    if executor_conversation_id is not None and auditor_conversation_id == executor_conversation_id:
        return "auditor_shares_executor_conversation"
    if (
        executor_model_ref is not None
        and auditor_model_ref is not None
        and executor_model_ref == auditor_model_ref
        and executor_conversation_id == auditor_conversation_id
    ):
        return "auditor_shares_executor_instance"
    return None


def propose_subtask(
    state: AuditedTaskState,
    *,
    subtask_id: str,
    description: str,
    acceptance_criteria: list[TaskCriterion] | None = None,
    scope: ScopeDescriptor | None = None,
    max_steps: int = 50,
) -> tuple[SubtaskContract | None, MeaEarlyStop | None]:
    """Manager role: emit a bounded SubtaskContract from residual budget.

    Returns (contract, None) or (None, early_stop).
    """

    stop = enforce_round_budget(state)
    if stop is not None:
        return None, stop

    criteria = acceptance_criteria or list(state.acceptance_criteria)
    # Residual slice: leave at least one attempt for later rounds when possible.
    residual = state.budget_residual
    slice_attempts = max(1, min(residual.max_attempts, max(1, residual.max_attempts // max(1, state.max_rounds - state.rounds_consumed))))
    sub_budget = residual.model_copy(update={"max_attempts": slice_attempts})

    contract = SubtaskContract(
        subtask_id=subtask_id,
        parent_state_version=state.version,
        description=description,
        acceptance_criteria=criteria,
        scope=scope or state.scope,
        budget=sub_budget,
        max_steps=max_steps,
    )
    return contract, None
