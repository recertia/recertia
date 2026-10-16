"""Backend resolution has one policy reader and refuses local where D2 says so."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from recertia.errors import ExecutionUnavailable
from recertia.ports.execution import LocalExecutionCapability
from recertia.settings import Settings
from recertia.solver.executor import build_executor, resolve_backend

REPO = Path(__file__).resolve().parents[3]


def test_default_backend_attribute_has_one_reader() -> None:
    hits: list[str] = []
    for path in (REPO / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "default_backend":
                hits.append(str(path.relative_to(REPO)))
    assert hits == ["src/recertia/solver/executor.py"]


def test_multi_node_capability_does_not_select_local() -> None:
    settings = Settings(deployment_mode="multi_node", execution_backend="container")
    with pytest.raises(ExecutionUnavailable):
        resolve_backend(
            settings,
            None,
            surface="cli",
            capability=LocalExecutionCapability(purpose="cli"),
        )


def test_api_local_requires_break_glass() -> None:
    blocked = Settings(
        execution_backend="container",
        execution_backend_was_set=True,
        api_allow_local_exec=False,
    )
    assert (
        resolve_backend(
            blocked,
            None,
            surface="api",
            capability=LocalExecutionCapability(purpose="api-break-glass"),
        )
        == "container"
    )
    allowed = Settings(
        execution_backend="container",
        execution_backend_was_set=True,
        api_allow_local_exec=True,
    )
    assert (
        resolve_backend(
            allowed,
            None,
            surface="api",
            capability=LocalExecutionCapability(purpose="api-break-glass"),
        )
        == "local"
    )


def test_golden_inherit_follows_settings() -> None:
    settings = Settings(
        golden_backend="inherit",
        execution_backend="container",
        execution_backend_was_set=True,
    )
    executor = build_executor(surface="golden", settings=settings)
    assert executor.config.backend == "container"


def test_context_override_selects_local_without_reading_policy() -> None:
    from recertia.solver.container import execution_backend_override

    settings = Settings(
        execution_backend="container",
        execution_backend_was_set=True,
    )
    with execution_backend_override("local"):
        assert (
            resolve_backend(settings, None, surface="cli", capability=None) == "local"
        )


def test_golden_local_without_capability_raises() -> None:
    settings = Settings(golden_backend="local", execution_backend="container")
    with pytest.raises(ExecutionUnavailable):
        build_executor(surface="golden", settings=settings)
