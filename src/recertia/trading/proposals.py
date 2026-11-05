"""Build improvement-plane proposals. They never write approved state."""

from __future__ import annotations

from recertia.jobs import JobResult, Proposal
from recertia.trading.bars import (
    RefuseError,
    default_bar_path,
    load_official_bars,
    tape_gate,
)

PROPOSAL_KINDS = frozenset({"rewrite", "gap_spawn", "cohort", "retirement"})


def trading_proposal(
    kind: str,
    skill_id: str,
    text: str,
    *,
    contribution: float | None = None,
    evidence_met: bool = False,
) -> Proposal:
    if kind not in PROPOSAL_KINDS:
        raise ValueError(f"unknown trading proposal {kind}")
    if kind == "retirement" and (
        contribution is None or not evidence_met or contribution >= 0
    ):
        raise ValueError("null contribution cannot retire")
    return Proposal(
        kind="curate",
        skill_id=skill_id,
        version=0,
        rationale=text,
        payload={
            "proposal_kind": kind,
            "status": "proposed",
            "contribution": contribution,
            "evidence_met": evidence_met,
            "writes_approved": False,
        },
    )


def run_trading_tape(path=None) -> JobResult:
    """Load the official tape. Do not invent positions and do not claim lift."""

    target = default_bar_path() if path is None else path
    try:
        bars = load_official_bars(target)
    except RefuseError as exc:
        return JobResult(job="trading", proposals=[], skipped=str(exc))
    reason = tape_gate(bars)
    if reason:
        return JobResult(job="trading", proposals=[], skipped=reason)
    return JobResult(job="trading", proposals=[], skipped="tape_loaded_no_positions")
