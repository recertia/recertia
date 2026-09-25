"""Statically enforces ADR-0005's T3 boundary: nothing a run or job can invoke may import a
T3 module (the eval harness, ablation sampler, promotion thresholds, sandbox policy, or this
boundary itself). Parses the AST rather than trusting convention, per the ADR's own mandate
("enforced by module boundaries and asserted in CI, not by convention").

``recertia.jobs`` and ``recertia.evals.ablation`` are scanned once present; this test covers
them by globbing rather than hardcoding paths.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from recertia.governance import T3_FORBIDDEN_FOR_RUNS_AND_JOBS

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARDED_PACKAGES = ("recertia/nodes", "recertia/jobs")


def _imported_module_names(source_path: Path) -> set[str]:
    tree = ast.parse(source_path.read_text(), filename=str(source_path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def _guarded_files() -> list[Path]:
    files: list[Path] = []
    for pkg in GUARDED_PACKAGES:
        pkg_dir = REPO_ROOT / "src" / pkg
        if pkg_dir.exists():
            files.extend(pkg_dir.rglob("*.py"))
    return files


@pytest.mark.parametrize("source_path", _guarded_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_t3_imports_in_runs_or_jobs(source_path: Path) -> None:
    imported = _imported_module_names(source_path)
    for forbidden in T3_FORBIDDEN_FOR_RUNS_AND_JOBS:
        violating = {
            name
            for name in imported
            if name == forbidden or name.startswith(forbidden + ".")
        }
        assert not violating, (
            f"{source_path.relative_to(REPO_ROOT)} imports {violating}, which is forbidden "
            f"under ADR-0005: T3 surfaces must be unreachable from any code path a run or job "
            f"can invoke."
        )


def test_guarded_files_is_non_empty() -> None:
    """A guard against this test silently checking nothing (e.g. a path typo)."""

    assert _guarded_files(), "expected at least recertia/nodes/*.py to exist and be scanned"


def test_nodes_must_not_import_replay_surface() -> None:
    """ADR-0011: solver nodes never import the offline replay package."""

    nodes_dir = REPO_ROOT / "src" / "recertia" / "nodes"
    for source_path in nodes_dir.rglob("*.py"):
        imported = _imported_module_names(source_path)
        violating = {
            name
            for name in imported
            if name == "recertia.replay" or name.startswith("recertia.replay.")
        }
        assert not violating, f"{source_path} imports replay surface {violating}"


def test_nodes_and_graph_must_not_import_lifecycle() -> None:
    """The walk cannot bench. Lifecycle writes stay on the improvement plane."""

    forbidden = "recertia.review.lifecycle"
    roots = (
        REPO_ROOT / "src" / "recertia" / "nodes",
        REPO_ROOT / "src" / "recertia" / "graph",
    )
    for root in roots:
        for source_path in root.rglob("*.py"):
            imported = _imported_module_names(source_path)
            violating = {
                name
                for name in imported
                if name == forbidden or name.startswith(forbidden + ".")
            }
            assert not violating, f"{source_path} imports lifecycle {violating}"


def test_retrieve_plan_solve_must_not_write_memory() -> None:
    """Read-only nodes cannot call ``.write(`` on episodic / facts."""

    names = ("retrieve.py", "plan.py")
    names += tuple(p.name for p in (REPO_ROOT / "src" / "recertia" / "nodes").glob("solve*.py"))
    nodes_dir = REPO_ROOT / "src" / "recertia" / "nodes"
    for name in names:
        source_path = nodes_dir / name
        if not source_path.exists():
            continue
        tree = ast.parse(source_path.read_text(), filename=str(source_path))
        writes = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "write"
            ):
                writes.append(node.lineno)
        assert writes == [], f"{source_path.name} calls .write( on lines {writes}"


def test_retrieve_cannot_maintain_the_index() -> None:
    source_path = REPO_ROOT / "src" / "recertia" / "nodes" / "retrieve.py"
    tree = ast.parse(source_path.read_text(), filename=str(source_path))
    forbidden = {"rebuild", "upsert"}
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            hits.append((node.attr, node.lineno))
    assert hits == [], f"retrieve.py touches index maintenance {hits}"
