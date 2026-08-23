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
from contracts.criteria import TaskCriterion


@dataclass(frozen=True)
class MeaEarlyStop:
    reason: str
    rounds_consumed: int
    max_rounds: int


def enforce_round_budget(state: AuditedTaskState) -> MeaEarlyStop | None:
    """Hard controller limit: max_rounds and residual budget.

    ``budget_residual`` is remaining capacity. Exhaustion is representable
    (Budget.max_attempts may be 0). Returns an early-stop reason when the
    next round must not start.
    """

    if state.rounds_consumed >= state.max_rounds:
        return MeaEarlyStop(
            reason="max_rounds_reached",
            rounds_consumed=state.rounds_consumed,
            max_rounds=state.max_rounds,
        )
    residual = state.budget_residual
    if residual.max_attempts <= 0:
        return MeaEarlyStop(
            reason="residual_attempts_exhausted",
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

    Fresh conversation is required. Distinct model_ref is preferred but not
    mandatory when conversations already differ (same model, new context is OK).
    """

    if auditor_conversation_id is None:
        return "auditor_missing_conversation"
    if (
        executor_conversation_id is not None
        and auditor_conversation_id == executor_conversation_id
    ):
        return "auditor_shares_executor_conversation"
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
    residual = state.budget_residual
    remaining_rounds = max(1, state.max_rounds - state.rounds_consumed)
    # Slice attempts across remaining rounds; leave capacity for later phases.
    slice_attempts = max(
        1,
        min(
            residual.max_attempts,
            max(1, residual.max_attempts // remaining_rounds),
        ),
    )
    # Also bound tool calls proportionally when residual has them.
    tool_slice = residual.max_tool_calls
    if residual.max_tool_calls > 0:
        tool_slice = max(
            1,
            min(
                residual.max_tool_calls,
                max(1, residual.max_tool_calls // remaining_rounds),
            ),
        )
    sub_budget = residual.model_copy(
        update={
            "max_attempts": slice_attempts,
            "max_tool_calls": tool_slice,
        }
    )

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
