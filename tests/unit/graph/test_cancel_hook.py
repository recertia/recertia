"""CancelHopHook stops a run; other hook errors stay inside the hop."""

from __future__ import annotations

from pathlib import Path

from recertia.graph.engine import GraphOrchestrator
from recertia.graph.hooks import CancelHopHook, Hop, RunCancelled
from recertia.nodes.context import NodeOutcome


def _hop() -> Hop:
    return Hop(run_id="r", tenant="t", node="intake", attempt_no=0, seq=0, workdir=Path("."))


class _Boom:
    def before(self, hop: Hop) -> None:
        del hop
        raise RuntimeError("hook-boom")

    def after(self, hop: Hop, outcome: NodeOutcome, *, hop_ms: float, idle_gap_ms: float) -> None:
        del hop, outcome, hop_ms, idle_gap_ms


def test_cancel_hook_propagates(tmp_path: Path) -> None:
    orch = GraphOrchestrator(tmp_path / "runs", hooks=[CancelHopHook(lambda: True)])
    try:
        try:
            orch._hop_before(_hop())
        except RunCancelled as exc:
            assert exc.args[0] == "r"
        else:
            raise AssertionError("expected RunCancelled")
    finally:
        orch.close()


def test_other_hook_errors_are_swallowed(tmp_path: Path) -> None:
    orch = GraphOrchestrator(tmp_path / "runs", hooks=[_Boom()])
    try:
        orch._hop_before(_hop())
    finally:
        orch.close()
