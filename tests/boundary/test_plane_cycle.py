"""The eval harness no longer constructs the graph engine, and memory promotion is a shim."""

from __future__ import annotations

import ast
from pathlib import Path

import grimp

ROOT = Path(__file__).resolve().parents[2]


def _imports(graph: grimp.ImportGraph, module: str) -> set[str]:
    return set(graph.find_modules_directly_imported_by(module))


def test_golden_does_not_import_the_graph_engine() -> None:
    graph = grimp.build_graph("recertia")
    assert "recertia.graph.engine" not in _imports(graph, "recertia.evals.golden")
    source = (ROOT / "src/recertia/evals/golden.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert "recertia.graph.engine" not in imported


def test_memory_promotion_is_a_shim_and_cut_edges_are_gone() -> None:
    source = (ROOT / "src/recertia/memory/procedural/promote.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    ]
    assert modules == ["recertia.review.promotion"]

    graph = grimp.build_graph("recertia")
    assert "recertia.evals.store" not in _imports(graph, "recertia.memory.procedural.active_set")
    assert "recertia.review.autonomy_config" not in _imports(
        graph, "recertia.memory.procedural.active_set"
    )
    assert "recertia.review.autonomy_config" not in _imports(
        graph, "recertia.memory.procedural.portfolio"
    )
    assert "recertia.evals.statistics" not in _imports(
        graph, "recertia.memory.procedural.contribution"
    )
    assert "recertia.evals.golden" not in _imports(graph, "recertia.memory.procedural.promote")
    assert "recertia.review.promotion" in _imports(graph, "recertia.memory.procedural.promote")
    assert "recertia.evals.golden" not in _imports(graph, "recertia.review")
    assert "recertia.evals.golden" in _imports(graph, "recertia.review.gate")
    assert "recertia.evals.golden" not in _imports(graph, "recertia.review.promotion")
