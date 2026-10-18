from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from contracts.budget import Budget
from contracts.failure import FailureVerdict
from contracts.patch import PatchTemplate
from contracts.run import RunManifest, RunState, Task, WorkspaceSnapshot
from recertia.memory.procedural.patch_templates import PatchTemplateStore
from recertia.nodes.context import NodeContext
from recertia.nodes.evolve import evolve


class _Workspaces:
    def __init__(self):
        self.restored = []

    def snapshot(self, workdir, run_id, *, attempt_no):
        return "snap"

    def restore(self, workdir, snapshot_ref):
        self.restored.append(snapshot_ref)


class _Ledger:
    def append(self, **kwargs):
        return None


class _Ops:
    def run_once(self, run_id, attempt_no, node, op_seq, fn, fencing_token=None, incarnation=1):
        return fn()


def test_evolve_applies_template_without_search(tmp_path: Path) -> None:
    store = PatchTemplateStore(tmp_path / "templates")
    template = PatchTemplate(
        template_id="t1",
        failure_signature="execution::boom",
        failure_class="execution",
        operations=[{"op": "retry"}],
        content_hash="abc",
        published_at=datetime.now(timezone.utc),
    )
    store.publish(template)
    ws = _Workspaces()
    ctx = NodeContext(
        run_id="r",
        attempt_no=1,
        node="evolve",
        workdir=tmp_path,
        workspaces=ws,
        ledger=_Ledger(),
        ops=_Ops(),
        patch_templates=store,
    )
    state = RunState(
        run_id="r",
        task=Task(task_id="t", request="do a repo chore please", submitted_at=datetime.now(timezone.utc)),
        manifest=RunManifest(),
        budget=Budget(),
        workspace_snapshots=[WorkspaceSnapshot(snapshot_ref="clean", attempt_no=0)],
        failure=FailureVerdict(failure_class="execution", evidence=["boom"], counts_against_trust=True),
    )
    outcome = evolve(state, ctx)
    assert "apply_template:t1" in (outcome.note or "")
    assert ws.restored == ["clean"]
    # One restore, no extra snapshot, no probe loop recorded on state.
    assert getattr(outcome.state, "evolve_search", None) is None
