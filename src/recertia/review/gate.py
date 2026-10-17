"""Golden gate runner. The eval package asks this factory for a nested orchestrator."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from recertia.ports.execution import CommandExecutor
from recertia.ports.orchestration import OrchestratorFactory


class GraphOrchestratorFactory:
    """Build a graph orchestrator and keep the golden executor beside it.

    Command dispatch inside the orchestrator still uses the configured runner.
    The executor is explicit so a later hop can call it without rebuilding policy.
    """

    def __init__(self, executor: CommandExecutor | None = None) -> None:
        self.executor = executor

    def build(self, runs_root: Path, *, policy: object | None = None) -> Any:
        from contracts.policy import Policy
        from recertia.graph.engine import GraphOrchestrator

        resolved = policy if isinstance(policy, Policy) else None
        orch = GraphOrchestrator(runs_root, policy=resolved)
        if self.executor is not None:
            setattr(orch, "executor", self.executor)
        return orch


def default_golden_factory() -> GraphOrchestratorFactory:
    """Factory for the process settings. Local golden runs carry a cli capability."""

    from recertia.ports.execution import LocalExecutionCapability
    from recertia.settings import load_settings
    from recertia.solver.executor import build_executor

    settings = load_settings()
    capability = None
    if settings.resolved_golden_backend() == "local":
        capability = LocalExecutionCapability(purpose="cli")
    executor = build_executor(surface="golden", settings=settings, capability=capability)
    return GraphOrchestratorFactory(executor)


class GoldenGateRunner:
    """``GateRunner`` that delegates fixture selection to the eval harness."""

    def __init__(self, factory: OrchestratorFactory | None = None) -> None:
        self.factory = factory

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
    ) -> Any:
        from recertia.evals.golden import select_and_run_gate

        factory = self.factory if self.factory is not None else default_golden_factory()
        return select_and_run_gate(
            version,  # type: ignore[arg-type]
            runs_root=runs_root,
            golden_root=golden_root,
            golden_dir=golden_dir,
            require_task_class_gate=require_task_class_gate,
            require_fixture=require_fixture,
            extra_golden_dirs=extra_golden_dirs,
            factory=factory,
        )


def discover_golden(
    golden_root: Path, skill_id: str, task_class: str = "repo-chore"
) -> Path | None:
    from recertia.evals.golden import discover_golden as impl

    return impl(golden_root, skill_id, task_class)


def discover_version_golden(
    golden_root: Path, skill_id: str, version: int, task_class: str = "repo-chore"
) -> Path | None:
    from recertia.evals.golden import discover_version_golden as impl

    return impl(golden_root, skill_id, version, task_class)
