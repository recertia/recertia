"""Run stores are injected. Bootstrap shares one workspace manager."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from recertia.bootstrap import build_default_orchestrator
from recertia.graph.engine import GraphOrchestrator
from recertia.graph.persistence import open_run_persistence
from recertia.memory.affordance import AffordanceStore
from recertia.solver.tools import ToolResult


def test_engine_does_not_construct_stores(tmp_path: Path) -> None:
    persistence = open_run_persistence(tmp_path / "runs")
    orch = GraphOrchestrator(tmp_path / "other", persistence=persistence)
    try:
        assert orch.checkpoints is persistence.checkpoints
        assert orch.ops is persistence.operations
        assert orch.ledger is persistence.ledger
        assert orch.workspaces is persistence.workspaces
        assert orch.trajectories is persistence.trajectories
        assert orch.audited_states is persistence.audited_states
    finally:
        orch.close()


def test_single_workspace_manager(tmp_path: Path) -> None:
    bundle = build_default_orchestrator(
        tmp_path / "runs",
        skills_root=tmp_path / "skills",
        facts_root=tmp_path / "facts",
    )
    try:
        orch = bundle.orchestrator
        assert orch.applicator is not None
        assert orch.workspaces is orch.applicator.workspaces
    finally:
        bundle.close()


def test_close_closes_the_affordance_store(tmp_path: Path) -> None:
    """REC-056d: the store's ``runtime.db`` writer does not outlive the orchestrator."""

    affordances = AffordanceStore(tmp_path / "runs" / "affordances.json")
    orch = GraphOrchestrator(tmp_path / "runs", affordances=affordances)
    try:
        affordances.record_tool(ToolResult(tool="t", ok=True))
        writer = affordances._writer
        assert writer is not None
    finally:
        orch.close()
    with pytest.raises(sqlite3.ProgrammingError):
        writer.execute("SELECT 1")
    orch.close()  # closing twice is safe
