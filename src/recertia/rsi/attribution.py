"""Turn-boundary credit. Observation tokens get nothing. Spec R8."""

from __future__ import annotations

from recertia.rsi.models import StepCredit, TurnView


def attribute(
    artifact_id: str,
    turns: list[TurnView],
    prioritized: float,
    gamma_turn: float = 0.95,
    constants_version: str = "",
) -> list[StepCredit]:
    if not (0.0 < gamma_turn <= 1.0):
        raise ValueError("gamma_turn must be in (0, 1]")
    weights = []
    for k, turn in enumerate(turns):
        if turn.decision_steps <= 0:
            weights.append(0.0)
            continue
        weights.append(turn.decision_steps * (gamma_turn ** k))
    total = sum(weights)
    if total <= 0 or prioritized == 0:
        return []
    credits = []
    for turn, weight in zip(turns, weights):
        if weight <= 0:
            continue
        credits.append(
            StepCredit(
                artifact_id=artifact_id,
                turn_id=turn.turn_id,
                credit=prioritized * (weight / total),
                gamma_turn=gamma_turn,
                constants_version=constants_version,
            )
        )
    return credits
