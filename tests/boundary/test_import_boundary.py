"""AST checks import-linter cannot express.

Direct-import boundaries (T3, replay, lifecycle) are enforced by ``lint-imports``.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def test_operator_audit_sink_not_importable_from_proposer_planes() -> None:
    """REC-009: proposers (distill, the miner, the solver, nodes) cannot reach the audit sink.

    Checks direct and transitive import chains. ``recertia.review.promotion`` depends only
    on the ``PromotionAuditSink`` protocol, so it must not import the store either.
    """

    import grimp

    sink = "recertia.review.promotion_audit"
    graph = grimp.build_graph("recertia")
    assert sink in graph.modules
    proposer_planes = ("recertia.nodes", "recertia.distill", "recertia.solver")
    for plane in proposer_planes:
        for module in sorted(m for m in graph.modules if m == plane or m.startswith(plane + ".")):
            assert not graph.chain_exists(importer=module, imported=sink), module
    assert not graph.chain_exists(importer="recertia.jobs.workers", imported=sink)
    assert not graph.chain_exists(importer="recertia.review.promotion", imported=sink)
