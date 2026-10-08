"""Paired contribution ledger helpers. ADR-0021, Proposed.

These functions do not read the run store and do not write T3.
A class interval is not a skill effect. a4 untested keeps the
retirement emitter off.
"""

from __future__ import annotations

hashlib
import json
import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from contracts.eval import (
    ConfidenceInterval,
    ContributionCell,
    LiftStatus,
)

_Z_95 = 1.959963984540054


def paired_discordant_interval(
    n_discordant_help: int,
    n_discordant_hurt: int,
    n_paired: int,
    *,
    level: float = 0.95,
) -> ConfidenceInterval | None:
    """Discordant-pair transform of a Wilson score interval.

    Point estimate is (b - c) / N. The Wilson interval on b / (b + c)
    is transformed by (2L - 1)(b + c) / N. Concordant pairs do not
    enter the interval. No discordant pairs returns None, which the
    caller marks insufficient_data rather than a zero effect.
    """

    if n_paired <= 0:
        return None
    discordant = n_discordant_help + n_discordant_hurt
    if discordant <= 0:
        return None
    z = _Z_95 if abs(level - 0.95) < 1e-9 else _z_for(level)
    p = n_discordant_help / discordant
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
        low=(2.0 * low_p - 1.0) * scale,
        high=(2.0 * high_p - 1.0) * scale,
        level=level,
        method="newcombe_paired_discordant",
    )


def cell_status(
    interval: ConfidenceInterval | None,
    *,
    n_paired: int,
    n_discordant: int,
    independent_runs: int,
    min_independent_runs: int = 5,
) -> LiftStatus:
    if n_paired == 0 or n_discordant == 0 or interval is None:
        return "insufficient_data"
    if independent_runs < min_independent_runs:
        return "low_run_count"
    if interval.low > 0:
        return "established_positive"
    if interval.high < 0:
        return "established_negative"
    return "not_established"


def protocol_hash(document: dict) -> str:
    """Canonical hash of a protocol registered before the first counted run."""

    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def hash_matches(expected: str | None, actual: str | None) -> bool:
    return bool(expected) and expected == actual


def holdout_blocks_distill(
    fixture_id: str | None,
    *,
    holdout_ids: Iterable[str],
    is_eval_fixture: bool = False,
) -> bool:
    """Same block as an eval fixture. Holdout chores never enter distill."""

    if is_eval_fixture:
        return True
    return fixture_id is not None and fixture_id in set(holdout_ids)


def mask_frozen_bundle(bundle: Sequence[str], skill_id: str) -> list[str]:
    """Copy the frozen bundle and drop one member. Does not refill."""

    return [item for item in bundle if item != skill_id]


def pair_on_task_id(
    on_rows: Sequence[dict],
    suppressed_rows: Sequence[dict],
) -> list[tuple[dict, dict]]:
    """Pair retrieval-on and retrieval-suppressed rows on task_id.

    Unmatched rows are dropped. This is not a class baseline subtraction.
    """

    suppressed = {row["task_id"]: row for row in suppressed_rows}
    pairs: list[tuple[dict, dict]] = []
    for row in on_rows:
        other = suppressed.get(row.get("task_id"))
        if other is not None:
            pairs.append((row, other))
    return pairs


def discordant_counts(pairs: Sequence[tuple[dict, dict]], key: str = "success") -> tuple[int, int, int]:
    help_n = 0
    hurt_n = 0
    for on_row, off_row in pairs:
        on_ok = bool(on_row.get(key))
        off_ok = bool(off_row.get(key))
        if on_ok and not off_ok:
            help_n += 1
        elif off_ok and not on_ok:
            hurt_n += 1
    return help_n, hurt_n, len(pairs)


@dataclass(frozen=True)
class RetirementProposal:
    skill_id: str
    reason: str
    writes_t3: bool = False
    emitted: bool = False


def retirement_proposal(
    cell: ContributionCell,
    *,
    shuffle_agrees: bool,
    threshold: float,
    judge_false_pass_rate: float | None,
    disabling_threshold: float,
) -> RetirementProposal | None:
    """Proposal row only. Emitter stays off while a4 is untested.

    writes_t3 is always false. Missing or over-threshold false-pass
    returns None rather than a retirement.
    """

    if judge_false_pass_rate is None:
        return None
    if judge_false_pass_rate > disabling_threshold:
        return None
    if cell.skill_id is None or cell.multiplicity != "secondary":
        return None
    if cell.estimand != "itt" or cell.pathway != "applied" or cell.holdout or cell.null_judge:
        return None
    if not shuffle_agrees:
        return None
    if cell.interval is None or cell.interval.high >= threshold:
        return None
    if cell.status != "established_negative":
        return None
    return RetirementProposal(
        skill_id=cell.skill_id,
        reason="secondary itt cell below threshold; shuffle agrees; a4 under threshold",
        writes_t3=False,
        emitted=True,
    )


def refused_sentences(result_library_allowed: bool, skill_allowed: bool) -> list[str]:
    lines = [
        "refused: the library helped, so this skill helped",
    ]
    if not result_library_allowed:
        lines.append("refused: class sentence (fixed-order-only, hash mismatch, or interval includes zero)")
    if not skill_allowed:
        lines.append(
            "refused: skill sentence (not secondary itt applied, holdout, judge-only, or a4 untested for retirement)"
        )
    return lines


def _z_for(level: float) -> float:
    if abs(level - 0.95) < 1e-9:
        return _Z_95
    raise ValueError("paired_discordant_interval currently supports level=0.95")
