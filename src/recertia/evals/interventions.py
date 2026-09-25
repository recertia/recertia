"""Controlled condensed-memory interventions (Zhao et al. 2026). Eval-only T3.

Never imported from ``recertia.nodes`` or ``recertia.jobs``. Production retrieve and
the skill store are not on this path: transformers return in-memory copies.
"""

from __future__ import annotations

from contracts.faithfulness import FaithfulnessIntervention
from contracts.skill import FailureMode, SkillVersion, Step

_PLACEHOLDER = "placeholder for emptied condensed memory"


def apply_intervention(
    version: SkillVersion,
    intervention: FaithfulnessIntervention,
    *,
    donor: SkillVersion | None = None,
) -> SkillVersion:
    if intervention == "empty":
        return apply_empty(version)
    if intervention == "corrupt":
        return apply_corrupt(version)
    if intervention == "irrelevant":
        if donor is None:
            raise ValueError("irrelevant intervention requires a donor skill from a distant task class")
        return apply_irrelevant(version, donor)
    if intervention == "filler":
        return apply_filler(version)
    raise ValueError(f"unknown intervention {intervention!r}")


def apply_empty(version: SkillVersion) -> SkillVersion:
    """Keep surface structure (step ids, tools) but drop the skill body."""

    steps = [
        step.model_copy(update={"intent": _PLACEHOLDER, "inputs": {}, "input_bindings": [], "outputs": []})
        for step in version.steps
    ]
    return version.model_copy(update={"steps": steps, "failure_modes": [], "preconditions": []})


def apply_corrupt(version: SkillVersion) -> SkillVersion:
    """Mutate steps, failure_modes, and preconditions while preserving ids and tool names."""

    steps = [_corrupt_step(step) for step in version.steps]
    modes = [
        FailureMode(symptom=_mutate(mode.response), response=_mutate(mode.symptom))
        for mode in version.failure_modes
    ] or [
        FailureMode(
            symptom="Inverted recovery path after corruption",
            response="Do not follow the original skill; the body was mutated on purpose",
        )
    ]
    preconditions = list(reversed(version.preconditions))
    return version.model_copy(update={"steps": steps, "failure_modes": modes, "preconditions": preconditions})


def apply_irrelevant(version: SkillVersion, donor: SkillVersion) -> SkillVersion:
    """Swap in a distant task-class body while keeping the original identity."""

    return version.model_copy(
        update={
            "steps": list(donor.steps),
            "failure_modes": list(donor.failure_modes),
            "preconditions": list(donor.preconditions),
            "parameters": list(donor.parameters),
            "certification_criteria": list(donor.certification_criteria),
            "intent": donor.intent,
            "title": donor.title,
        }
    )


def apply_filler(version: SkillVersion) -> SkillVersion:
    """Replace semantic text with same-length non-semantic tokens."""

    steps = [
        step.model_copy(
            update={
                "intent": _filler_text(step.intent),
                "inputs": {key: _filler_value(value) for key, value in step.inputs.items()},
            }
        )
        for step in version.steps
    ]
    modes = [
        FailureMode(symptom=_filler_text(mode.symptom), response=_filler_text(mode.response))
        for mode in version.failure_modes
    ]
    return version.model_copy(update={"steps": steps, "failure_modes": modes})


def _corrupt_step(step: Step) -> Step:
    inputs = dict(step.inputs)
    if "command" in inputs and isinstance(inputs["command"], str):
        inputs["command"] = _mutate(str(inputs["command"]))
    return step.model_copy(update={"intent": _mutate(step.intent), "inputs": inputs})


def _mutate(text: str) -> str:
    tokens = text.split()
    if len(tokens) < 2:
        return f"NOT {text}" if text else "NOT"
    return " ".join(reversed(tokens))


def _filler_text(text: str) -> str:
    n = max(len(text), 8)
    return ("xxxx " * ((n // 5) + 1))[:n]


def _filler_value(value: object) -> object:
    if isinstance(value, str):
        return _filler_text(value)
    return value
