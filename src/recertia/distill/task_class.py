"""Snake Goal/golden class ↔ kebab SkillVersion.task_class (ADR-0019).

``COMPUTER_USE_TASK_CLASSES`` is the only allowed computer-use set on ingest/distill.
Kebab lives only on ``SkillVersion.task_class``; this module is the single mapper.
"""

from __future__ import annotations

from contracts.policy import COMPUTER_USE_TASK_CLASSES


def is_computer_use_class(task_class: str) -> bool:
    return task_class in COMPUTER_USE_TASK_CLASSES


def skill_task_class(task_class: str) -> str:
    """Map snake Goal/golden class onto kebab SkillVersion.task_class."""

    return task_class.replace("_", "-")


def computer_use_class_help() -> str:
    return "|".join(COMPUTER_USE_TASK_CLASSES)
