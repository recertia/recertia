"""Phase 3: Systems / Tower projections and stuck detection for MEA Goals.

Pure functions over AuditedTaskState. No new persona; read-only projection.
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.audited_task_state import AuditedTaskState, Blocker


@dataclass(frozen=True)
class MeaSystemsBrief:
    state_id: str
    goal_id: str
    version: int
    current_phase: str
    rounds_consumed: int
    rounds_remaining: int
    max_rounds: int
    residual_attempts: int
    residual_tool_calls: int
    residual_cost_usd: float | None
    evidence_coverage: float  # verified decisions / acceptance criteria (0..1+)
    blocker_ids: list[str]
    stuck: bool
    stuck_reason: str | None
    last_auditor_report_id: str | None


def evidence_coverage(state: AuditedTaskState) -> float:
    """Ratio of verified decisions to locked acceptance criteria.

    Can exceed 1.0 when multiple decisions map to the same criterion area.
    """
    n_crit = len(state.acceptance_criteria)
    if n_crit == 0:
        return 0.0
    return len(state.verified_decisions) / n_crit


def detect_stuck(
    state: AuditedTaskState,
    *,
    previous: AuditedTaskState | None = None,
    residual_rounds_floor: int = 2,
) -> tuple[bool, str | None]:
    """Stuck when:

    1. The same blocker_id persists across two consecutive accepted versions, or
    2. Residual rounds are at/below floor and no progress on acceptance criteria
       (no new verified decisions since previous, or previous is None and no decisions).
    """
    if previous is not None and previous.version + 1 == state.version:
        prev_ids = {b.blocker_id for b in previous.current_blockers}
        curr_ids = {b.blocker_id for b in state.current_blockers}
        shared = prev_ids & curr_ids
        if shared:
            return True, f"persistent_blocker:{sorted(shared)[0]}"

        if residual_rounds_floor >= 0:
            remaining = state.max_rounds - state.rounds_consumed
            if remaining <= residual_rounds_floor:
                prev_n = len(previous.verified_decisions)
                curr_n = len(state.verified_decisions)
                if curr_n <= prev_n:
                    return True, "no_progress_near_round_floor"

    remaining = state.max_rounds - state.rounds_consumed
    if remaining <= residual_rounds_floor and not state.verified_decisions:
        return True, "no_progress_near_round_floor"

    return False, None


def build_mea_systems_brief(
    state: AuditedTaskState,
    *,
    previous: AuditedTaskState | None = None,
    residual_rounds_floor: int = 2,
) -> MeaSystemsBrief:
    stuck, reason = detect_stuck(
        state, previous=previous, residual_rounds_floor=residual_rounds_floor
    )
    residual = state.budget_residual
    return MeaSystemsBrief(
        state_id=state.state_id,
        goal_id=state.goal_id,
        version=state.version,
        current_phase=state.current_phase,
        rounds_consumed=state.rounds_consumed,
        rounds_remaining=max(0, state.max_rounds - state.rounds_consumed),
        max_rounds=state.max_rounds,
        residual_attempts=residual.max_attempts,
        residual_tool_calls=residual.max_tool_calls,
        residual_cost_usd=residual.max_cost_usd,
        evidence_coverage=evidence_coverage(state),
        blocker_ids=[b.blocker_id for b in state.current_blockers],
        stuck=stuck,
        stuck_reason=reason,
        last_auditor_report_id=state.last_auditor_report_id,
    )
