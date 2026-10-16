"""The import-linter ignore list is a ratchet: it matches real imports and cannot grow quietly."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import grimp

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE = REPO_ROOT / "tests" / "boundary" / "import-linter-baseline.txt"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _baseline_edges() -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for raw in BASELINE.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if "->" not in line:
            continue
        importer, imported = (part.strip() for part in line.split("->", 1))
        edges.add((importer, imported))
    return edges


def _config_edges() -> set[tuple[str, str]]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    edges: set[tuple[str, str]] = set()
    for contract in data["tool"]["importlinter"]["contracts"]:
        for raw in contract.get("ignore_imports", []):
            importer, imported = (part.strip() for part in raw.split("->", 1))
            edges.add((importer, imported))
    return edges


def _direct_imports() -> set[tuple[str, str]]:
    graph = grimp.build_graph("recertia")
    edges: set[tuple[str, str]] = set()
    for importer in graph.modules:
        for imported in graph.find_modules_directly_imported_by(importer):
            edges.add((importer, imported))
    return edges


def test_baseline_ignores_match_real_imports() -> None:
    direct = _direct_imports()
    missing = sorted(f"{a} -> {b}" for a, b in _baseline_edges() - direct)
    assert missing == [], f"ignore lines no longer match a real import: {missing}"


def test_no_unlisted_ignore() -> None:
    assert _baseline_edges() == _config_edges()


def test_kept_ast_boundaries_still_parse() -> None:
    """The two AST checks import-linter cannot express stay in the boundary module."""

    source = (REPO_ROOT / "tests" / "boundary" / "test_import_boundary.py").read_text()
    tree = ast.parse(source)
    names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert "test_retrieve_plan_solve_must_not_write_memory" in names
    assert "test_retrieve_cannot_maintain_the_index" in names
    assert "test_no_t3_imports_in_runs_or_jobs" not in names
