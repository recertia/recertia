"""Computer-use golden task class descriptors (Phase 2).

Three classes from the 2026-08-22 external-trajectories plan, expressible in
the existing criteria language. All support clean memory-off control runs.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ComputerUseTaskClass = Literal[
    "bug_reproduction",
    "playtest_operator",
    "docs_auditor",
]


class GoldenTaskClassDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_class: ComputerUseTaskClass
    primary_criteria_shape: str
    required_artifacts: list[str]
    supports_control_arm: bool = True
    supports_mea: bool = True
    notes: str | None = None


GOLDEN_TASK_CLASSES: dict[ComputerUseTaskClass, GoldenTaskClassDescriptor] = {
    "bug_reproduction": GoldenTaskClassDescriptor(
        task_class="bug_reproduction",
        primary_criteria_shape="command + file/screenshot hash + network notes",
        required_artifacts=["steps", "screenshots", "har", "terminal_log"],
        notes="Reproduce a reported UI/API defect with attributable evidence",
    ),
    "playtest_operator": GoldenTaskClassDescriptor(
        task_class="playtest_operator",
        primary_criteria_shape="UI assertion sequence + final state predicate",
        required_artifacts=["step_log", "screenshots", "final_state_snapshot"],
        notes="Operator-driven playtest of a multi-step UI flow",
    ),
    "docs_auditor": GoldenTaskClassDescriptor(
        task_class="docs_auditor",
        primary_criteria_shape="product-vs-docs diff + non-regression assertions",
        required_artifacts=["before_after_diffs", "missing_page_list"],
        notes="Detect product/docs drift with machine-checkable evidence",
    ),
}


def mea_derived_promotion_evidence_floor() -> int:
    """Identical to ordinary skills — no fast-track, no reduced floor.

    Returns the evidence_floor applications required before contribution
    intervals may drive retirement or promotion decisions. MEA provenance
    does not lower this bar.
    """
    return 5  # matches Policy default used elsewhere; never reduced for MEA
