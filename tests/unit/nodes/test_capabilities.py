"""Warn-mode grants name fields a node is not supposed to see."""

from __future__ import annotations

from pathlib import Path

from recertia.nodes.capabilities import undeclared_capabilities
from recertia.nodes.context import NodeContext


def _ctx(**extra: object) -> NodeContext:
    return NodeContext(
        run_id="r",
        attempt_no=0,
        node="retrieve",
        workdir=Path("."),
        workspaces=object(),  # type: ignore[arg-type]
        ledger=object(),  # type: ignore[arg-type]
        ops=object(),  # type: ignore[arg-type]
        **extra,  # type: ignore[arg-type]
    )


def test_retrieve_context_has_no_index_maintenance() -> None:
    gaps = undeclared_capabilities("retrieve", _ctx(index=object(), retriever=object()))
    assert "index" in gaps
    assert "retriever" not in gaps


def test_validate_context_has_no_solver_model() -> None:
    gaps = undeclared_capabilities(
        "validate",
        _ctx(model=object(), verifier_model=object(), reviewer=None, index=None),
    )
    assert gaps == ["model"]
