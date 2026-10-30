"""#160: the faithfulness runner closes its orchestrator and index when setup raises.

`_default_runner` builds a `GraphOrchestrator`, then reads the fixture and builds the task
before `start`. A failure in between (here an unreadable `task.json`) must still close the
orchestrator and the skill index. Fixed in #213; this pins it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from recertia.cli.faithfulness_cmd import _default_runner
from recertia.graph.engine import GraphOrchestrator
from recertia.retrieval.index import SkillIndex


def test_faithfulness_runner_closes_orchestrator_when_setup_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[str] = []
    started: list[str] = []
    orch_close = GraphOrchestrator.close
    index_close = SkillIndex.close

    def record_orch_close(self: GraphOrchestrator) -> None:
        closed.append("orchestrator")
        orch_close(self)

    def record_index_close(self: SkillIndex) -> None:
        closed.append("index")
        index_close(self)

    def record_start(self: GraphOrchestrator, *args: object, **kwargs: object) -> object:
        started.append("start")
        raise AssertionError("start must not be reached")

    monkeypatch.setattr(GraphOrchestrator, "close", record_orch_close)
    monkeypatch.setattr(GraphOrchestrator, "start", record_start)
    monkeypatch.setattr(SkillIndex, "close", record_index_close)

    golden_root = tmp_path / "golden"
    fixture_dir = golden_root / "demo" / "fx-1"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "task.json").write_text("{not json", encoding="utf-8")

    runner = _default_runner(runs_root=tmp_path / "runs", golden_root=golden_root, task_class="demo")
    with pytest.raises(json.JSONDecodeError):
        runner("run-1", "fx-1", overlay=None, bundle_hook=None)

    assert started == []
    assert "orchestrator" in closed
    assert "index" in closed
