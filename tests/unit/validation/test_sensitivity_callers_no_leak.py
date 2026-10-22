"""REC-046: the real negative-fixture callers leave no recertia-neg-* directory behind."""

from __future__ import annotations

import hashlib
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.criteria import TaskCriterion
from contracts.goal import DesiredState, Goal
from contracts.run import RunManifest, RunState, Task
from recertia.graph.ops import OperationLedger
from recertia.jobs import Proposal
from recertia.jobs.workers import draft_from_mine_proposal
from recertia.ledger import HashChainLedger
from recertia.nodes.context import NodeContext
from recertia.nodes.intake import intake
from recertia.validation.critic import propose_criteria, refine_goal_criteria
from recertia.validation.sensitivity import author_sensitivity_proof
from recertia.workspace import WorkspaceManager

EMPTY_FIXTURE = f"sha256:{hashlib.sha256().hexdigest()}"


def _neg_dirs(root: Path) -> list[str]:
    return sorted(p.name for p in root.rglob("recertia-neg-*"))


@pytest.fixture
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point tempfile at an empty directory so leftovers can be listed."""

    root = tmp_path / "tempfile-root"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def _intake_ctx(tmp_path: Path) -> tuple[Path, NodeContext]:
    """A task workdir that already holds ``output.txt``, and an intake context for it."""

    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "output.txt").write_text("already solved")
    ctx = NodeContext(
        run_id="rec046",
        attempt_no=0,
        node="intake",
        workdir=workdir,
        workspaces=WorkspaceManager(tmp_path / "snapshots"),
        ledger=HashChainLedger(tmp_path / "ledger.jsonl"),
        ops=OperationLedger(tmp_path / "ops.db"),
    )
    return workdir, ctx


def _intake_state(task: Task) -> RunState:
    return RunState(run_id="rec046", task=task, manifest=RunManifest())


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"arxiv_id": "2610.00001", "title": "A paper title for the miner", "abstract": "x"},
    ],
    ids=["artifact-hint", "paper"],
)
def test_mined_draft_leaves_no_negative_fixture(temp_root: Path, payload: dict) -> None:
    proposal = Proposal(
        kind="mine", skill_id="mined-demo", version=1, rationale="rec046", payload=payload
    )

    version = draft_from_mine_proposal(proposal)

    proof = version.certification_criteria[0].sensitivity_proof
    assert proof is not None
    assert proof.checked_against == EMPTY_FIXTURE
    assert _neg_dirs(temp_root) == []


def test_critic_leaves_no_negative_fixture(temp_root: Path) -> None:
    proposed = propose_criteria("write output.txt")
    required = [
        TaskCriterion(id=f"c{i}", kind="command", run="test -f out.txt", source="caller")
        for i in range(2)
    ]
    refined = refine_goal_criteria(required)

    proofs = [c.sensitivity_proof for c in [*proposed, *refined]]
    assert len(proofs) == 3
    for proof in proofs:
        assert proof is not None
        assert proof.rejected is True
        assert proof.checked_against == EMPTY_FIXTURE
    assert _neg_dirs(temp_root) == []


def test_intake_keeps_negative_fixture_out_of_task_workdir(
    tmp_path: Path, temp_root: Path
) -> None:
    workdir, ctx = _intake_ctx(tmp_path)
    state = _intake_state(Task(
        task_id="rec046",
        request="write output.txt",
        task_class="repo-chore",
        submitted_at=datetime.now(timezone.utc),
    ))

    out = intake(state, ctx)

    (criterion,) = out.state.criteria
    assert criterion.sensitivity_proof is not None
    # The negative fixture is empty and separate from the task workdir, so a criterion
    # that the real workdir already satisfies is still rejected there.
    assert criterion.sensitivity_proof.rejected is True
    assert criterion.sensitivity_proof.checked_against == EMPTY_FIXTURE
    assert sorted(p.name for p in workdir.iterdir()) == ["output.txt"]
    assert _neg_dirs(temp_root) == []


def test_intake_negative_fixture_is_not_inside_task_workdir(
    tmp_path: Path, temp_root: Path
) -> None:
    """Pin the fixture's location: ``..`` of the fixture must not be the task workdir."""

    workdir, ctx = _intake_ctx(tmp_path)
    probe = "test -f ../output.txt"
    # The probe discriminates: from a fixture inside the workdir it finds output.txt.
    inside = workdir / "inside-fixture"
    inside.mkdir()
    assert author_sensitivity_proof(
        TaskCriterion(id="probe", kind="command", run=probe, source="caller"),
        negative_workdir=inside,
    ).rejected is False
    inside.rmdir()
    assert not (temp_root / "output.txt").exists()
    state = _intake_state(Task(
        task_id="rec046",
        goal=Goal(desired=[DesiredState(id="parent-probe", kind="command", run=probe)]),
        task_class="repo-chore",
        submitted_at=datetime.now(timezone.utc),
    ))

    out = intake(state, ctx)

    (criterion,) = out.state.criteria
    assert criterion.run == probe
    assert criterion.sensitivity_proof is not None
    assert criterion.sensitivity_proof.rejected is True
    assert sorted(p.name for p in workdir.iterdir()) == ["output.txt"]
    assert _neg_dirs(temp_root) == []
