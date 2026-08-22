"""First-class golden task classes (ADR-0019 computer-use goldens)."""

from __future__ import annotations

COMPUTER_USE_TASK_CLASSES: tuple[str, ...] = (
    "bug-reproduction",
    "playtest-operator",
    "docs-auditor",
)

KNOWN_TASK_CLASSES: tuple[str, ...] = (
    "repo-chore",
    "research-synthesis",
    *COMPUTER_USE_TASK_CLASSES,
)
