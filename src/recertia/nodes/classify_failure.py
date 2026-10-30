"""``classify_failure``: assign a failure class from the taxonomy (specs §4, §16)."""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

from contracts.failure import FailureVerdict
from contracts.graph import legal_routes
from contracts.run import RunState
from recertia.nodes.context import NodeContext, NodeOutcome

if TYPE_CHECKING:
    from recertia.memory.affordance import AffordanceStore

_log = logging.getLogger(__name__)


def classify_failure(state: RunState, ctx: NodeContext) -> NodeOutcome:
    signal = state.failure_signal
    if signal is None:
        raise ValueError("classify_failure requires a FailureSignal on the run state (ADR-0008)")

    if (
        signal.class_hint == "budget"
        or state.spent.attempts >= state.budget.max_attempts
        or "budget exhausted" in signal.detail
    ):
        evidence = (
            signal.detail
            if signal.class_hint == "budget" or "budget exhausted" in signal.detail
            else (
                f"spent.attempts={state.spent.attempts} >= "
                f"budget.max_attempts={state.budget.max_attempts}"
            )
        )
        failure = FailureVerdict(
            failure_class="budget",
            evidence=[evidence],
            counts_against_trust=False,
            escalate_to_human=False,
        )
    elif signal.source == "join":
        failure = FailureVerdict(
            failure_class="merge",
            evidence=[signal.detail],
            counts_against_trust=False,
            escalate_to_human=False,
        )
    elif signal.source == "solver" and _is_tool_failure(state, ctx, signal.detail):
        failure = FailureVerdict(
            failure_class="tool",
            evidence=[signal.detail],
            counts_against_trust=False,
            escalate_to_human=False,
        )
    elif signal.source == "solver" and "claim timeout" in signal.detail.lower():
        failure = FailureVerdict(
            failure_class="merge",
            evidence=[signal.detail],
            counts_against_trust=False,
            escalate_to_human=False,
        )
    elif signal.source == "solver" and _is_environment_failure(signal.detail):
        failure = FailureVerdict(
            failure_class="environment",
            evidence=[signal.detail],
            counts_against_trust=False,
            escalate_to_human=False,
        )
    elif signal.source == "solver":
        # Generic solver failure without affordance match → tool if flaky registry says so,
        # else treat as tool for unexpected nonzero exits from the tool runtime.
        failure = FailureVerdict(
            failure_class="tool",
            evidence=[signal.detail],
            counts_against_trust=False,
            escalate_to_human=False,
        )
    elif _has_invalid_sensitivity_proof(state):
        failure = FailureVerdict(
            failure_class="criteria",
            evidence=_invalid_proof_evidence(state),
            counts_against_trust=False,
            escalate_to_human=True,
        )
    elif state.chosen is not None and not state.results and signal.source == "validator":
        failure = FailureVerdict(
            failure_class="retrieval",
            evidence=[signal.detail],
            implicated_skill={"skill_id": state.chosen.skill_id, "version": state.chosen.version},
            counts_against_trust=True,
            escalate_to_human=False,
        )
    else:
        failure = FailureVerdict(
            failure_class="execution",
            evidence=[r.criterion_id for r in state.results if not r.passed] or [signal.detail],
            implicated_skill=(
                {"skill_id": state.chosen.skill_id, "version": state.chosen.version}
                if state.chosen
                else None
            ),
            counts_against_trust=True,
            escalate_to_human=False,
        )

    assert failure.is_consistent, f"classify_failure produced an inconsistent verdict: {failure!r}"

    new_state = state.model_copy(update={"failure": failure})
    legal = legal_routes("classify_failure", new_state)
    assert len(legal) == 1, f"ambiguous routes out of classify_failure: {[r.predicate_name for r in legal]}"
    return NodeOutcome(
        state=new_state, route=legal[0].predicate_name, note=f"classified as {failure.failure_class!r}"
    )


def _is_tool_failure(state: RunState, ctx: NodeContext, detail: str) -> bool:
    if ctx.tools is None and ctx.affordances is None:
        return True  # M0/M1 path: solver-raised ⇒ tool
    # Known flaky tool or matching error signature → tool class (no trust impact).
    if ctx.tools is not None:
        for name in ctx.tools.names():
            if ctx.tools.is_flaky(name) and name in detail:
                return True
            sig = ctx.tools.match_error_signature(name, detail)
            if sig:
                return True
    if ctx.affordances is not None and _affordances_name_a_tool(ctx.affordances, detail):
        return True
    return "tool=" in detail or "flaky" in detail.lower()


def _affordances_name_a_tool(affordances: AffordanceStore, detail: str) -> bool:
    """True when a known-flaky tool or a recorded error signature matches ``detail``."""

    try:
        for name in list(affordances.tools):
            if affordances.is_known_flaky(name) and name in detail:
                return True
            if affordances.matches_error_signature(name, detail):
                return True
    except sqlite3.Error as exc:
        # Affordance history only sharpens the verdict; without it the detail decides.
        _log.warning("affordance lookup skipped: %s: %s", type(exc).__name__, exc)
    return False


def _is_environment_failure(detail: str) -> bool:
    return detail.startswith("environment:") or "before first productive" in detail


def _has_invalid_sensitivity_proof(state: RunState) -> bool:
    failed_ids = {r.criterion_id for r in state.results if not r.passed}
    for criterion in state.criteria:
        if criterion.id in failed_ids and criterion.is_required and criterion.sensitivity_proof is not None:
            if not criterion.sensitivity_proof.rejected:
                return True
    return False


def _invalid_proof_evidence(state: RunState) -> list[str]:
    failed_ids = {r.criterion_id for r in state.results if not r.passed}
    return [
        f"{c.id}: sensitivity_proof.rejected=False (does not reject its own negative fixture)"
        for c in state.criteria
        if c.id in failed_ids and c.sensitivity_proof is not None and not c.sensitivity_proof.rejected
    ]
