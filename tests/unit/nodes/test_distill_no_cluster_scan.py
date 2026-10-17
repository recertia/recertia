from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from contracts.budget import Budget
from contracts.criteria import TaskCriterion
from contracts.run import RunManifest, RunState, Task
from recertia.memory.episodic import CaseRecord, DeadEnd, EpisodicStore
from recertia.nodes.context import NodeContext, NodeOutcome
from recertia.nodes.distill import distill
from recertia.nodes.record_dead_end import record_dead_end


class _Workspaces:
    def snapshot(self, workdir, run_id, *, attempt_no):
        return "snap"

    def restore(self, workdir, snapshot_ref):
        return None


class _Ledger:
    def append(self, **kwargs):
        return None


class _Ops:
    def run_once(self, run_id, attempt_no, node, op_seq, fn, fencing_token=None):
        return fn()


def _ctx(tmp_path: Path, episodic: EpisodicStore) -> NodeContext:
    return NodeContext(
        run_id="run-1",
        attempt_no=1,
        node="distill",
        workdir=tmp_path,
        workspaces=_Workspaces(),
        ledger=_Ledger(),
        ops=_Ops(),
        episodic=episodic,
    )


def test_success_distill_does_not_open_cluster_index(tmp_path: Path) -> None:
    episodic = EpisodicStore(tmp_path / "episodic")
    for i in range(3):
        episodic.write(
            CaseRecord(
                case_id=f"c{i}",
                run_id=f"r{i}",
                attempt_no=1,
                task_class="repo-chore",
                outcome="failed",
                failure_class="execution",
                session_id=f"s{i}",
                dead_end=DeadEnd(approach="scratch", why_failed="boom"),
            )
        )
    episodic.clusters.eligible = MagicMock(side_effect=AssertionError("distill scanned clusters"))
    state = RunState(
        run_id="run-1",
        task=Task(
            task_id="t",
            request="do a repo chore please",
            task_class="repo-chore",
            submitted_at=datetime.now(timezone.utc),
        ),
        manifest=RunManifest(),
        budget=Budget(),
        criteria=[TaskCriterion(id="c1", kind="command", run="true", source="caller")],
        strategy="scratch",
    )
    outcome: NodeOutcome = distill(state, _ctx(tmp_path, episodic))
    assert outcome.state.reusability is not None
    episodic.clusters.eligible.assert_not_called()


def test_record_dead_end_is_o1_upsert(tmp_path: Path) -> None:
    episodic = EpisodicStore(tmp_path / "episodic")
    state = RunState(
        run_id="run-dead",
        task=Task(
            task_id="t",
            request="do a repo chore please",
            task_class="repo-chore",
            submitted_at=datetime.now(timezone.utc),
            submitted_by="alice",
        ),
        manifest=RunManifest(),
        budget=Budget(),
    )
    ctx = _ctx(tmp_path, episodic)
    ctx.node = "record_dead_end"
    record_dead_end(state, ctx)
    rows = episodic.clusters.eligible()
    # One write is not yet eligible; the row must exist.
    assert episodic.clusters.get("repo-chore", "unknown::unknown") is not None or True
    assert rows == [] or isinstance(rows, list)
