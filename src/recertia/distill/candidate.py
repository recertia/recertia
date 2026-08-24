"""Shared candidate-skill gates for success / import / paper distill.

Refuse secrets, true-noop steps, and judge-only / ``true`` certification.
Promotion stays outside this module (golden gate / reviewer).
"""

from __future__ import annotations

from contracts.skill import SkillVersion, Step
from recertia.memory.procedural.hygiene import require_clean

_NOOP_ACTIONS = frozenset({"true", "open", "click", "type", "scroll", "wait", "navigate"})


class DistillRejected(ValueError):
    """Authoring refused: true-noop, secrets, or missing command criterion."""


def step_command(step: Step) -> str:
    return str((step.inputs or {}).get("command") or "").strip()


def is_noop_command(command: str) -> bool:
    """``true``, empty, and UI/wait actions are not replayable shell work."""

    cmd = command.strip()
    if not cmd:
        return True
    return cmd.lower() in _NOOP_ACTIONS


def is_noop_criterion(criterion: object) -> bool:
    """Judge-only, empty, or ``true`` command runs cannot certify a candidate."""

    kind = getattr(criterion, "kind", None)
    run = (getattr(criterion, "run", None) or "").strip()
    if kind == "judge":
        return True
    if kind != "command":
        return True
    return (not run) or run == "true"


def assert_non_noop_skill(version: SkillVersion) -> None:
    """Raise if the skill has no replayable step or no command criterion."""

    if not any(not is_noop_command(step_command(step)) for step in version.steps):
        raise DistillRejected(
            "no replayable shell steps; refuse to author a true-noop skill"
        )
    certs = list(version.certification_criteria or [])
    if not any(not is_noop_criterion(c) for c in certs):
        raise DistillRejected(
            "no command criterion; refuse to author a true-noop skill"
        )


def assert_candidate_hygiene(version: SkillVersion) -> SkillVersion:
    """Non-noop gates + secret/PII scan. Returns a hygiene-stamped skill."""

    assert_non_noop_skill(version)
    try:
        return require_clean(version)
    except ValueError as exc:
        raise DistillRejected(str(exc)) from exc
