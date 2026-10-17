"""Hop hooks observe every node. A raising hook does not fail the run."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from contracts.criteria import TaskCriterion
from contracts.run import Task
from recertia.graph.engine import GraphOrchestrator
from recertia.graph.hooks import Hop, TelemetryHopHook
from recertia.nodes.context import NodeOutcome


def _start(orch: GraphOrchestrator, tmp_path: Path, run_id: str) -> None:
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    orch.start(
        run_id,
        Task(task_id="t1", request="pause", submitted_at=datetime.now(timezone.utc)),
        [
            TaskCriterion(
                id="output-exists",
                kind="command",
                run="test -f output.txt",
                source="caller",
                weight=1.0,
            )
        ],
        workdir=workdir,
        max_steps=3,
    )


class _Recording:
    def __init__(self) -> None:
        self.nodes: list[str] = []

    def before(self, hop: Hop) -> None:
        self.nodes.append(hop.node)

    def after(
        self,
        hop: Hop,
        outcome: NodeOutcome,
        *,
        hop_ms: float,
        idle_gap_ms: float,
    ) -> None:
        del hop, outcome, hop_ms, idle_gap_ms


class _Boom:
    def before(self, hop: Hop) -> None:
        del hop
        raise RuntimeError("hook-boom")

    def after(
        self,
        hop: Hop,
        outcome: NodeOutcome,
        *,
        hop_ms: float,
        idle_gap_ms: float,
    ) -> None:
        del hop, outcome, hop_ms, idle_gap_ms


def test_hook_sees_every_node_in_order(tmp_path: Path) -> None:
    seen = _Recording()
    orch = GraphOrchestrator(tmp_path / "runs", hooks=[seen, TelemetryHopHook()])
    try:
        _start(orch, tmp_path, "run-hooks")
    finally:
        orch.close()
    assert seen.nodes == ["intake", "retrieve", "plan"]


def test_hook_exception_does_not_fail_the_run(tmp_path: Path) -> None:
    orch = GraphOrchestrator(tmp_path / "runs", hooks=[_Boom(), TelemetryHopHook()])
    try:
        _start(orch, tmp_path, "run-boom")
        assert orch.checkpoints.latest("run-boom") is not None
    finally:
        orch.close()
