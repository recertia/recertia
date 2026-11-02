"""M6–M9 done-when suites (fan-out, jobs, composition, second domain / hardening)."""

from __future__ import annotations

import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.budget import Budget
from contracts.criteria import SensitivityProof, SkillCertificationCriterion, TaskCriterion
from contracts.run import Task
from contracts.skill import Hygiene, Provenance, SkillUse, SkillVersion, Step
from recertia.graph.engine import GraphOrchestrator
from recertia.jobs import JobBudget, JobRunner
from recertia.jobs.workers import (
    curator_active_set_and_dedup,
    draft_from_mine_proposal,
    mine_from_repo_hints,
    propose_parallelise,
    recertify_stale,
)
from recertia.ledger import HashChainLedger
from recertia.memory.procedural.allocate import allocate_next_version, write_version_exclusive
from recertia.memory.procedural.composition import (
    CompositionError,
    mean_composition_depth,
    quarantine_child_blocks_parents,
    resolve_uses,
)
from recertia.memory.procedural.seeds import seed_approved_for_tests
from recertia.memory.procedural.store import SkillStore
from recertia.nodes.context import NodeContext
from recertia.nodes.plan import plan
from recertia.review.policy import PolicyError, approve_policy_change, propose_policy_change


def _proven(cmd: str = "true") -> TaskCriterion:
    return TaskCriterion(
        id="gate",
        kind="command",
        run=cmd,
        source="caller",
        weight=1.0,
        sensitivity_proof=SensitivityProof(
            criterion_id="gate",
            negative_fixture="empty",
            rejected=True,
            checked_at=datetime.now(timezone.utc),
        ),
    )


def _skill(
    skill_id: str,
    *,
    uses: list[SkillUse] | None = None,
    task_class: str = "repo-chore",
) -> SkillVersion:
    proof = SensitivityProof(
        criterion_id="ok",
        negative_fixture="empty",
        rejected=True,
        checked_at=datetime.now(timezone.utc),
    )
    return SkillVersion(
        skill_id=skill_id,
        version=1,
        title=f"Title for {skill_id} ok",
        intent=f"Intent long enough covering {skill_id} for composition tests.",
        task_class=task_class,
        uses=uses or [],
        steps=[
            Step(
                id="step_1",
                tool="shell",
                intent="Trivial shell step used by composition and job fixtures",
                inputs={"command": "true"},
            )
        ],
        certification_criteria=[
            SkillCertificationCriterion(
                id="ok", kind="command", run="true", sensitivity_proof=proof, preregistered=True
            )
        ],
        provenance=Provenance(
            distilled_from_run="t",
            distilled_at=datetime.now(timezone.utc),
            curation="human_authored",
            authoring_prior_version="ap",
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=datetime.now(timezone.utc)),
    )


# --- M6 ---


def test_portfolio_fan_out_respects_budget_and_selects(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    orch = GraphOrchestrator(tmp_path / "runs")
    try:
        state = orch.start(
            "m6-port",
            Task(
                task_id="t",
                request="PORTFOLIO: do the chore",
                task_class="repo-chore",
                submitted_at=datetime.now(timezone.utc),
            ),
            [_proven("true"), _proven("true").model_copy(update={"id": "gate2"})],
            budget=Budget(max_attempts=2, max_cost_usd=1.0),
            workdir=work,
            script=["true"],
        )
    finally:
        orch.close()
    assert state.strategy == "portfolio"
    assert state.branches
    assert state.merge_audits
    assert state.merge_audits[-1].is_complete
    assert sum(b.cost_usd or 0 for b in state.branches) <= 1.0
    assert any(b.selected for b in state.branches)
    assert state.terminal in ("solved", "unsolved", "error")  # distill may one_off


def test_decomposition_refused_when_criteria_not_partitionable(tmp_path: Path) -> None:
    from recertia.graph.ops import OperationLedger
    from recertia.ledger import HashChainLedger
    from recertia.workspace import WorkspaceManager

    work = tmp_path / "w"
    work.mkdir()
    with closing(OperationLedger(tmp_path / "o.db")) as ops:
        ctx = NodeContext(
            run_id="r",
            attempt_no=0,
            node="plan",
            workdir=work,
            workspaces=WorkspaceManager(tmp_path / "snap"),
            ledger=HashChainLedger(tmp_path / "l.jsonl"),
            ops=ops,
        )
        from contracts.run import RunState

        state = RunState(
            run_id="r",
            task=Task(
                task_id="t",
                request="DECOMPOSE: only one criterion",
                submitted_at=datetime.now(timezone.utc),
            ),
            criteria=[_proven()],
        )
        outcome = plan(state, ctx)
        assert outcome.state.strategy == "scratch"
        assert "refused" in (outcome.state.strategy_reason or "")


def test_merge_gap_is_visible(tmp_path: Path) -> None:
    from contracts.branch import BranchState
    from contracts.run import RunState
    from recertia.graph.ops import OperationLedger
    from recertia.nodes.join import join
    from recertia.workspace import WorkspaceManager

    work = tmp_path / "w"
    work.mkdir()
    with closing(OperationLedger(tmp_path / "o.db")) as ops:
        ctx = NodeContext(
            run_id="r",
            attempt_no=1,
            node="join",
            workdir=work,
            workspaces=WorkspaceManager(tmp_path / "snap"),
            ledger=HashChainLedger(tmp_path / "l.jsonl"),
            ops=ops,
        )
        state = RunState(
            run_id="r",
            task=Task(task_id="t", request="x", submitted_at=datetime.now(timezone.utc)),
            strategy="portfolio",
            branches=[
                BranchState(
                    branch_id="b1",
                    strategy="scratch",
                    workspace_ref="w1",
                    budget=Budget(),
                    status="succeeded",
                ),
                BranchState(
                    branch_id="b2",
                    strategy="scratch",
                    workspace_ref="w2",
                    budget=Budget(),
                    status="dispatched",  # missing
                ),
            ],
        )
        outcome = join(state, ctx)
        assert outcome.route == "otherwise"
        assert outcome.state.failure is not None
        assert outcome.state.failure.failure_class == "merge"
        assert outcome.state.merge_audits[-1].missing == ["b2"]


# --- M7 ---


def test_jobs_propose_but_cannot_skip_golden_gate(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    runner = JobRunner(store, golden_root=None)
    result = runner.run(
        "miner",
        lambda: mine_from_repo_hints(store, hints=["Add CI workflow from HISTORY"]),
        budget=JobBudget(max_proposals=5),
    )
    assert result.proposals
    draft = draft_from_mine_proposal(result.proposals[0])
    submitted = runner.submit_proposal(result.proposals[0], draft)
    assert submitted.startswith("candidate:")
    assert store.get_status(draft.skill_id, draft.version).lifecycle == "candidate"
    assert store.get_status(draft.skill_id, draft.version).active is False


def test_recertifier_and_parallelise_proposals(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    v = _skill("needs-tool")
    seed_approved_for_tests(store, v, active=True)
    props = recertify_stale(store, tool_upgraded="pytest")
    assert props
    assert store.get_status("needs-tool", 1).lifecycle == "needs_recert"
    assert propose_parallelise("needs-tool", 1, fake_edge_failures=5)
    assert not propose_parallelise("needs-tool", 1, fake_edge_failures=2)
    curator_active_set_and_dedup(store)


# --- M8 ---


def test_composition_and_quarantine_blocks_parents(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    child = _skill("child-skill")
    parent = _skill("parent-skill", uses=[SkillUse(skill_id="child-skill", version=1)])
    for ver in (child, parent):
        seed_approved_for_tests(store, ver, active=True)
    resolved = resolve_uses(store, parent)
    assert resolved[0].skill_id == "child-skill"
    assert mean_composition_depth(store) >= 0.5
    touched = quarantine_child_blocks_parents(store, "child-skill", 1)
    assert store.get_status("child-skill", 1).lifecycle == "quarantined"
    assert any(s.skill_id == "parent-skill" and s.lifecycle == "needs_recert" for s in touched)
    with pytest.raises(CompositionError, match="quarantined"):
        resolve_uses(store, store.get_version("parent-skill", 1))


# --- M9 ---


def test_second_domain_fixture_exists_without_schema_change() -> None:
    root = Path("evals/golden/research-synthesis/draft-structured-brief")
    assert (root / "task.json").exists()
    assert (root / "workspace" / "notes.md").exists()


def test_policy_change_requires_approver_and_eval(tmp_path: Path) -> None:
    ledger = HashChainLedger(tmp_path / "ledger.jsonl")
    doc = tmp_path / "policy.json"
    doc.write_text('{"version":"1"}\n', encoding="utf-8")
    proposal = propose_policy_change(
        proposal_id="p1",
        document_path=doc,
        before=doc.read_text(),
        after='{"version":"2"}\n',
        eval_comparison="lift unchanged on repo-chore golden",
    )
    with pytest.raises(PolicyError, match="approver"):
        approve_policy_change(proposal, approver="", ledger=ledger)
    approved = approve_policy_change(
        proposal,
        approver="alice",
        ledger=ledger,
        apply_to=doc,
        new_contents='{"version":"2"}\n',
    )
    assert approved.status == "approved"
    assert doc.read_text() == '{"version":"2"}\n'
    assert ledger.entries()[-1].action == "policy_change"


def test_concurrent_version_allocation_has_no_gaps_or_dupes(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    errors: list[BaseException] = []
    written: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            for _ in range(5):
                ver_no = allocate_next_version(store, "alloc-skill")
                ver = _skill("alloc-skill").model_copy(update={"version": ver_no, "skill_id": "alloc-skill"})
                # title/intent uniqueness not required
                write_version_exclusive(store, ver)
                with lock:
                    written.append(ver_no)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert sorted(written) == list(range(1, len(written) + 1))
    assert len(set(written)) == len(written)
