"""Factory for a nested run. The concrete orchestrator stays in the graph package."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from recertia.ports.execution import CommandExecutor


class OrchestratorFactory(Protocol):
    """Build a starter with ``start`` and ``close``. Holds the golden executor."""

    executor: CommandExecutor | None

    def build(self, runs_root: Path, *, policy: object | None = None) -> Any: ...


class GateRunner(Protocol):
    """Run a skill's golden fixtures through an injected factory."""

    def run(
        self,
        version: object,
        *,
        runs_root: Path,
        golden_root: Path | None = None,
        golden_dir: Path | None = None,
        require_task_class_gate: bool = False,
        require_fixture: bool = False,
        extra_golden_dirs: list[Path] | None = None,
    ) -> Any: ...
