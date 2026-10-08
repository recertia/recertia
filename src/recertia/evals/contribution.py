"""Paired contribution ledger (ADR-0021, Proposed).

Does not establish a1. Does not write T3. A retirement row is a proposal.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from contracts.eval import (
    ConfidenceInterval,
    ContributionCell,
    LiftStatus,
)


class ContributionProtocol(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_class: str
    chore_ids: list[str]
    holdout_ids: list[str] = Field(default_factory=list)
    skill_ids: list[str]
    shuffle_seeds: list[int]
    model_pin: str
    criteria_hash: str
    tau: float = 0.0
    k: int = Field(ge=1)

    def canonical(self) -> str:
        payload = self.model_dump(mode="json")
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def protocol_hash(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()


class PairRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    arm: str
    success: bool
    skill_id: str | None = None
    pathway: str = "never_retrieved"
    order_arm: str = "fixed"
    shuffle_index: int | None = None
    attempts: int | None = None
    cost_usd: float | None = None
    valid_non_judge: bool = True
    stratum: str = "repo-chore"


class RetirementProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str
    stratum: str
    emitted: bool
    writes_t3: bool = False
    reason: str


def load_protocol(path: Path) -> ContributionProtocol:
    return ContributionProtocol.model_validate_json(path.read_text(encoding="utf-8"))


def reject_holdout(protocol: ContributionProtocol, fixture_id: str | None) -> str | None:
    """Return a refusal if distill must not write this fixture. None means allowed."""

    if fixture_id and fixture_id in set(protocol.holdout_ids):
        return f"holdout {fixture_id} is blocked at distill"
    return None


def mask_bundle(bundle: list[str], skill_id: str) -> list[str]:
    """Copy the frozen bundle and drop one member. Do not substitute."""

    return [item for item in bundle if item != skill_id]


def paired_discordant_interval(
    n_paired: int,
    n_help: int,
    n_hurt: int,
    *,
    level: float = 0.95,
) -> ConfidenceInterval | None:
    if n_paired <= 0 or n_help < 0 or n_hurt < 0 or n_help + n_hurt > n_paired:
        return None
    discordant = n_help + n_hurt
    if discordant == 0:
        return None
    if level != 0.95:
        raise ValueError("unsupported confidence level; use 0.95")
    z = 1.959963984540054
    p = n_help / discordant
    z2 = z * z
    denom = 1.0 + z2 / discordant
    centre = (p + z2 / (2 * discordant)) / denom
    half = (z / denom) * math.sqrt(
        p * (1.0 - p) / discordant + z2 / (4 * discordant * discordant)
    )
    low_p = max(0.0, centre - half)
    high_p = min(1.0, centre + half)
    scale = discordant / n_paired
    return ConfidenceInterval(
        low=(2 * low_p - 1) * scale,
        high=(2 * high_p - 1) * scale,
        level=level,
        method="newcombe_paired_discordant",
    )


def classify_paired(
    n_paired: int,
    n_help: int,
    n_hurt: int,
    *,
    min_independent_runs: int = 5,
    pathway: str = "applied",
) -> tuple[float | None, ConfidenceInterval | None, LiftStatus]:
    if pathway == "never_retrieved" or n_paired == 0:
        return None, None, "insufficient_data"
    interval = paired_discordant_interval(n_paired, n_help, n_hurt)
    if interval is None:
        return None, None, "insufficient_data"
    estimate = (n_help - n_hurt) / n_paired
    if n_paired < min_independent_runs and (interval.low > 0 or interval.high < 0):
        return estimate, interval, "low_run_count"
    if interval.low > 0:
        return estimate, interval, "established_positive"
    if interval.high < 0:
        return estimate, interval, "established_negative"
    return estimate, interval, "not_established"


def holm_keeps(p_values: list[tuple[str, float]], alpha: float = 0.05) -> set[str]:
    """Return skill ids that survive Holm. Exploratory ids must not be passed in."""

    ordered = sorted(p_values, key=lambda item: item[1])
    m = len(ordered)
    kept: set[str] = set()
    for i, (skill_id, p_value) in enumerate(ordered, start=1):
        if p_value <= alpha / (m - i + 1):
            kept.add(skill_id)
        else:
            break
    return kept


def mcnemar_p(n_help: int, n_hurt: int) -> float | None:
    discordant = n_help + n_hurt
    if discordant == 0:
        return None
    # Two-sided exact test on the discordant split, p=0.5.
    k = min(n_help, n_hurt)
    tail = sum(math.comb(discordant, i) for i in range(k + 1)) / (2**discordant)
    return min(1.0, 2 * tail)


def pair_rows(rows: Iterable[PairRow], *, protocol: ContributionProtocol) -> list[ContributionCell]:
    """Pair on task_id. Concordant pairs do not move the estimate."""

    grouped: dict[tuple[str, str, str, str], list[PairRow]] = {}
    for row in rows:
        key = (row.stratum, row.order_arm, row.skill_id or "", row.task_id)
        grouped.setdefault(key, []).append(row)
    cells: list[ContributionCell] = []
    buckets: dict[tuple[str, str, str], list[tuple[bool, bool, int, float]]] = {}
    for (stratum, order_arm, skill_id, _task_id), group in grouped.items():
        on = next((r for r in group if r.arm == "on"), None)
        masked = next((r for r in group if r.arm == "masked"), None)
        if on is None or masked is None:
            continue
        if not on.valid_non_judge or not masked.valid_non_judge:
            continue
        attempts = 0
        cost = 0.0
        if on.attempts is not None and masked.attempts is not None:
            attempts = on.attempts - masked.attempts
        if on.cost_usd is not None and masked.cost_usd is not None:
            cost = on.cost_usd - masked.cost_usd
        buckets.setdefault((stratum, order_arm, skill_id), []).append(
            (on.success, masked.success, attempts, cost)
        )
    registered = set(protocol.skill_ids)
    for (stratum, order_arm, skill_id), pairs in buckets.items():
        help_n = sum(1 for on, masked, _, _ in pairs if on and not masked)
        hurt_n = sum(1 for on, masked, _, _ in pairs if masked and not on)
        pathway = "applied" if skill_id in registered else "never_retrieved"
        estimate, interval, status = classify_paired(
            len(pairs), help_n, hurt_n, pathway=pathway if skill_id else "applied"
        )
        multiplicity = "secondary" if skill_id else "primary"
        if skill_id and skill_id not in registered:
            multiplicity = "exploratory"
        cells.append(
            ContributionCell(
                skill_id=skill_id or None,
                stratum=stratum,
                order_arm=order_arm,  # type: ignore[arg-type]
                n_paired=len(pairs),
                n_discordant_help=help_n,
                n_discordant_hurt=hurt_n,
                estimate=estimate,
                interval=interval,
                status=status,
                pathway=pathway if skill_id else "applied",  # type: ignore[arg-type]
                estimand="itt",
                multiplicity=multiplicity,  # type: ignore[arg-type]
                holdout=False,
                protocol_hash=protocol.protocol_hash(),
                attempts_delta=sum(item[2] for item in pairs) / len(pairs),
                cost_delta_usd=sum(item[3] for item in pairs) / len(pairs),
            )
        )
    return cells


def skill_claim_allowed(
    cell: ContributionCell,
    *,
    other_order: ContributionCell | None,
    protocol_hash: str | None,
    holm_kept: set[str],
) -> bool:
    if cell.multiplicity != "secondary" or cell.skill_id is None:
        return False
    if cell.holdout or cell.null_judge or cell.estimand != "itt":
        return False
    if cell.pathway != "applied":
        return False
    if protocol_hash is None or cell.protocol_hash != protocol_hash:
        return False
    if cell.status != "established_positive":
        return False
    if other_order is None or other_order.order_arm == cell.order_arm:
        return False
    if other_order.status != "established_positive":
        return False
    return cell.skill_id in holm_kept


def refused_sentences(
    cell: ContributionCell,
    *,
    other_order: ContributionCell | None,
    a4_measured: bool,
    protocol_ok: bool,
) -> list[str]:
    refused: list[str] = []
    if not protocol_ok:
        refused.append("Protocol hash mismatch. Established language refused.")
    if cell.pathway != "applied":
        refused.append("Skill was not applied. A retrieved-unused or never-retrieved row is not a skill effect.")
    if cell.estimand != "itt":
        refused.append("Per-protocol row alone does not establish a skill effect.")
    if cell.multiplicity == "exploratory":
        refused.append("Exploratory cell. Not a pre-registered skill contrast.")
    if cell.holdout:
        refused.append("Holdout row. Not a promotion row.")
    if cell.null_judge:
        refused.append("Judge-only cell is null.")
    if other_order is None or other_order.order_arm == cell.order_arm:
        refused.append("Fixed-order-only gain. The shuffle row is missing or is the same arm.")
    elif other_order.status != cell.status:
        refused.append("Order strata disagree. A curriculum is not a skill effect.")
    if cell.status == "established_negative" and not a4_measured:
        refused.append("Retirement proposal refused while a4 is untested.")
    return refused


def retirement_proposal(
    cell: ContributionCell,
    *,
    other_order: ContributionCell | None,
    a4_measured: bool,
    false_pass_rate: float | None,
    tau: float,
) -> RetirementProposal | None:
    """Emit a proposal only. writes_t3 stays false. Emitter stays off while a4 is untested."""

    if cell.skill_id is None:
        return None
    if not a4_measured or false_pass_rate is None:
        return RetirementProposal(
            skill_id=cell.skill_id,
            stratum=cell.stratum,
            emitted=False,
            writes_t3=False,
            reason="emitter off: a4 is untested",
        )
    if false_pass_rate > tau:
        return RetirementProposal(
            skill_id=cell.skill_id,
            stratum=cell.stratum,
            emitted=False,
            writes_t3=False,
            reason="emitter off: false-pass rate is above the disabling threshold",
        )
    if cell.status != "established_negative" or cell.interval is None or cell.interval.high >= -tau:
        return None
    if other_order is None or other_order.status != "established_negative":
        return RetirementProposal(
            skill_id=cell.skill_id,
            stratum=cell.stratum,
            emitted=False,
            writes_t3=False,
            reason="shuffle row does not agree",
        )
    return RetirementProposal(
        skill_id=cell.skill_id,
        stratum=cell.stratum,
        emitted=True,
        writes_t3=False,
        reason="proposal only; jobs do not write T3",
    )
