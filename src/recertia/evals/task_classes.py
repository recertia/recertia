"""First-class golden task classes (ADR-0019 computer-use goldens)."""

from __future__ import annotations

from contracts.policy import COMPUTER_USE_TASK_CLASSES

__all__ = ["COMPUTER_USE_TASK_CLASSES", "KNOWN_TASK_CLASSES"]

KNOWN_TASK_CLASSES: tuple[str, ...] = (
    "repo-chore",
    "research-synthesis",
    *COMPUTER_USE_TASK_CLASSES,
)
