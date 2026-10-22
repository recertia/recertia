"""Critic pass: propose or refine TaskCriteria (M3 + Variant B Goal path)."""

from __future__ import annotations

from contracts.criteria import TaskCriterion
from recertia.validation.sensitivity import author_sensitivity_proof


def propose_criteria(request: str) -> list[TaskCriterion]:
    """Author a minimal required command criterion from the request text (legacy path).

    The proof is authored against an empty negative fixture so vacuous ``true`` criteria are
    detected (``rejected=False``) and remain advisory under validate's §15.2 rule.
    """

    check = _guess_check(request)
    criterion = TaskCriterion(
        id="critic-primary",
        kind="command",
        run=check,
        source="critic",
        weight=1.0,
    )
    proof = author_sensitivity_proof(criterion)
    return [criterion.model_copy(update={"sensitivity_proof": proof})]


def refine_goal_criteria(criteria: list[TaskCriterion]) -> list[TaskCriterion]:
    """Ensure every required criterion carries a sensitivity proof.

    Used after Goal compilation. Does not invent new success conditions; only authors
    missing proofs against a negative fixture.
    """

    refined: list[TaskCriterion] = []
    for c in criteria:
        if c.weight >= 1.0 and c.sensitivity_proof is None:
            proof = author_sensitivity_proof(c)
            refined.append(c.model_copy(update={"sensitivity_proof": proof}))
        else:
            refined.append(c)
    return refined


def _guess_check(request: str) -> str:
    lower = (request or "").lower()
    if "output.txt" in lower:
        return "test -f output.txt"
    if ".gitignore" in lower:
        return "test -f .gitignore"
    if "readme" in lower:
        return "test -f README.md"
    if "editorconfig" in lower or ".editorconfig" in lower:
        return "test -f .editorconfig"
    # Default: require *some* file exists — empty workspace fails this.
    return "test -n \"$(ls -A . 2>/dev/null)\""
