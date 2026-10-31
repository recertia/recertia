from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from recertia.cli.main import app

runner = CliRunner()


def _python(code: str) -> str:
    """A shell command that runs ``code`` with this test's interpreter.

    The executable is quoted and ``code`` uses only single quotes, so the same string works
    under ``sh`` and ``cmd.exe`` and does not depend on which interpreter ``PATH`` finds.
    """

    return f'"{sys.executable}" -c "{code}"'


def _spec(workdir: Path) -> dict:
    return {
        "task": {"request": "write output.txt"},
        "criteria": [
            {
                "id": "output-exists",
                "kind": "command",
                "run": _python("import os, sys; sys.exit(0 if os.path.isfile('output.txt') else 1)"),
                "source": "caller",
                "weight": 1.0,
                "sensitivity_proof": {
                    "criterion_id": "output-exists",
                    "negative_fixture": "empty workspace",
                    "rejected": True,
                    "checked_at": "2026-01-01T00:00:00Z",
                },
            }
        ],
        "script": [_python("open('output.txt','w').write('done')")],
        "workdir": str(workdir),
    }


def test_run_and_show_and_ledger_verify(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_spec(workdir)))
    runs_root = tmp_path / "runs"

    result = runner.invoke(
        app, ["run", "--spec", str(spec_path), "--runs-root", str(runs_root), "--run-id", "cli-run-1"]
    )
    assert result.exit_code == 0, result.output
    assert "terminal=solved" in result.output
    assert (workdir / "output.txt").read_text() == "done"

    show_result = runner.invoke(
        app, ["runs", "show", "cli-run-1", "--runs-root", str(runs_root), "--route-log"]
    )
    assert show_result.exit_code == 0, show_result.output
    assert "terminal=solved" in show_result.output
    assert "distill" in show_result.output
    assert "advisory (actual=pass)" in show_result.output

    verify_result = runner.invoke(app, ["ledger", "verify", "--runs-root", str(runs_root)])
    assert verify_result.exit_code == 0, verify_result.output
    assert "ledger OK" in verify_result.output


def test_ledger_verify_on_empty_runs_root(tmp_path: Path) -> None:
    result = runner.invoke(app, ["ledger", "verify", "--runs-root", str(tmp_path / "nothing-here")])
    assert result.exit_code == 0
    assert "0 entries" in result.output


def test_run_reports_unreservable_portfolio_budget_without_routing_error(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    spec = _spec(workdir)
    spec["task"]["request"] = "PORTFOLIO: write output.txt"
    spec["budget"] = {"max_attempts": 1}
    spec_path = tmp_path / "budget-spec.json"
    spec_path.write_text(json.dumps(spec))

    result = runner.invoke(
        app,
        [
            "run",
            "--spec",
            str(spec_path),
            "--runs-root",
            str(tmp_path / "runs"),
            "--run-id",
            "cli-budget",
        ],
    )

    assert result.exit_code == 1
    assert "terminal=unsolved" in result.output
    assert "failure_class=budget" in result.output
    assert "RoutingError" not in result.output


def test_skills_promote_via_cli(tmp_path: Path) -> None:
    from recertia.memory.procedural.seeds import SEED_SKILLS, seed_stats, seed_status_draft
    from recertia.memory.procedural.store import SkillStore

    version = next(s for s in SEED_SKILLS if s.skill_id == "add-gitignore-entry")
    # Golden gate requires hashed rejecting sensitivity evidence on certification criteria.
    criteria = []
    for c in version.certification_criteria:
        proof = c.sensitivity_proof
        if proof is not None and proof.evidence_hash is None:
            proof = proof.model_copy(update={"evidence_hash": "sha256:test-promote-evidence"})
        criteria.append(c.model_copy(update={"sensitivity_proof": proof}))
    version = version.model_copy(update={"certification_criteria": criteria})

    store = SkillStore(tmp_path / "skills")
    store.write_version(version)
    store.write_status(seed_status_draft(version))
    store.write_stats(seed_stats(version))

    golden = tmp_path / "golden"
    (golden / "workspace").mkdir(parents=True)
    (golden / "workspace" / ".gitignore").write_text("*.egg-info/\n.venv/\n")
    (golden / "task.json").write_text(
        json.dumps(
            {
                "request": "Add *.pyc to the repository .gitignore",
                "task_class": "repo-chore",
                "expected_skill_id": "add-gitignore-entry",
            }
        )
        + "\n"
    )
    (golden / "expect.json").write_text(json.dumps({"terminal": "solved"}) + "\n")

    result = runner.invoke(
        app,
        [
            "skills",
            "promote",
            "add-gitignore-entry",
            "--version",
            "1",
            "--skills-root",
            str(tmp_path / "skills"),
            "--golden-dir",
            str(golden),
            "--runs-root",
            str(tmp_path / "runs"),
            "--log-dir",
            str(tmp_path / "logs"),
            "--audit-root",
            str(tmp_path / "operator-audit"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "lifecycle=approved" in result.output
    assert store.get_status("add-gitignore-entry", 1).lifecycle == "approved"


def test_policy_show_without_subcommand() -> None:
    result = runner.invoke(app, ["policy"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "version" in payload
    assert "improvement" in payload


def test_skills_list_and_show(tmp_path: Path) -> None:
    from recertia.memory.procedural.seeds import SEED_SKILLS, seed_stats, seed_status_draft
    from recertia.memory.procedural.store import SkillStore

    version = next(s for s in SEED_SKILLS if s.skill_id == "add-gitignore-entry")
    store = SkillStore(tmp_path / "skills")
    store.write_version(version)
    store.write_status(seed_status_draft(version))
    store.write_stats(seed_stats(version))

    listed = runner.invoke(
        app, ["skills", "list", "--skills-root", str(tmp_path / "skills"), "--lifecycle", "draft"]
    )
    assert listed.exit_code == 0, listed.output
    assert "add-gitignore-entry@1" in listed.output
    assert "lifecycle=draft" in listed.output

    shown = runner.invoke(
        app,
        [
            "skills",
            "show",
            "add-gitignore-entry@1",
            "--skills-root",
            str(tmp_path / "skills"),
        ],
    )
    assert shown.exit_code == 0, shown.output
    payload = json.loads(shown.output)
    assert payload["version"]["skill_id"] == "add-gitignore-entry"
    assert payload["status"]["lifecycle"] == "draft"


def test_policy_propose_does_not_apply(tmp_path: Path) -> None:
    from recertia.proposals.store import ProposalStore

    repo_policy = Path(__file__).resolve().parents[2] / "policy" / "default.json"
    before = repo_policy.read_bytes()
    runs_root = tmp_path / "root"
    result = runner.invoke(
        app,
        [
            "policy",
            "propose",
            "retrieval.min_score=0.60",
            "--eval-compare",
            "synthetic compare",
            "--runs-root",
            str(runs_root),
            "--tenant",
            "default",
        ],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["kind"] == "policy"
    assert body["payload"]["applied"] is False
    assert body["payload"]["policy_diff"]["retrieval"]["min_score"] == 0.6
    store = ProposalStore(runs_root / "proposals.sqlite")
    pending = store.list(tenant_id="default", status="pending")
    assert any(p.proposal_id == body["proposal_id"] for p in pending)
    store.close()
    assert repo_policy.read_bytes() == before


def test_review_approve_does_not_write_lifecycle_approved(tmp_path: Path) -> None:
    from recertia.memory.procedural.seeds import SEED_SKILLS, seed_stats, seed_status_draft
    from recertia.memory.procedural.store import SkillStore
    from recertia.proposals.store import ProposalRecord, ProposalStore

    version = next(s for s in SEED_SKILLS if s.skill_id == "add-gitignore-entry")
    skills = SkillStore(tmp_path / "skills")
    skills.write_version(version)
    skills.write_status(seed_status_draft(version))
    skills.write_stats(seed_stats(version))

    runs_root = tmp_path / "root"
    store = ProposalStore(runs_root / "proposals.sqlite")
    rec = store.add(
        ProposalRecord(
            proposal_id="dec1",
            kind="skill",
            skill_id="add-gitignore-entry",
            version=1,
            rationale="review",
            tenant_id="default",
        )
    )
    store.close()

    queued = runner.invoke(
        app, ["review", "queue", "--runs-root", str(runs_root), "--tenant", "default"]
    )
    assert queued.exit_code == 0, queued.output
    assert "dec1" in queued.output

    approved = runner.invoke(
        app,
        [
            "review",
            "approve",
            rec.proposal_id,
            "--note",
            "looks good",
            "--runs-root",
            str(runs_root),
            "--tenant",
            "default",
        ],
    )
    assert approved.exit_code == 0, approved.output
    assert json.loads(approved.output)["status"] == "approved"
    assert skills.get_status("add-gitignore-entry", 1).lifecycle == "draft"


@pytest.mark.parametrize(
    "vanish_after_read", [1, 2], ids=["after-the-library-read", "between-decide-read-and-update"]
)
def test_review_approve_on_a_proposal_that_vanishes_says_review_not_found_and_writes_no_ledger_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vanish_after_read: int
) -> None:
    # Read 1 is the library's own lookup and read 2 is decide's "pending" read. Deleting the row
    # right after read 1 makes decide's read find no row; right after read 2, its UPDATE changes
    # no row and its re-read finds none. Either way: "review not found", exit 1, no traceback.
    import sqlite3
    from contextlib import closing

    from recertia.ledger import HashChainLedger
    from recertia.proposals.store import ProposalRecord, ProposalStore

    runs_root = tmp_path / "root"
    store = ProposalStore(runs_root / "proposals.sqlite")
    store.add(
        ProposalRecord(
            proposal_id="gone",
            kind="skill",
            skill_id="add-gitignore-entry",
            version=1,
            rationale="review",
            tenant_id="default",
        )
    )
    store.close()
    reads: list[str] = []
    read = ProposalStore.get

    def get_then_vanish(self: ProposalStore, proposal_id: str, *, tenant_id: str) -> ProposalRecord | None:
        rec = read(self, proposal_id, tenant_id=tenant_id)
        reads.append("found" if rec is not None else "missing")
        if len(reads) == vanish_after_read:
            # As another process would: through its own connection on the same file.
            with closing(sqlite3.connect(runs_root / "proposals.sqlite")) as conn, conn:
                conn.execute("DELETE FROM proposals WHERE proposal_id = ?", (proposal_id,))
        return rec

    monkeypatch.setattr(ProposalStore, "get", get_then_vanish)
    approved = runner.invoke(
        app,
        ["review", "approve", "gone", "--runs-root", str(runs_root), "--tenant", "default"],
    )

    # A clean exit 1 (typer.Exit), not an exception the runner caught (that also reports exit 1).
    assert isinstance(approved.exception, SystemExit), repr(approved.exception)
    assert approved.exit_code == 1, (approved.exit_code, approved.output, approved.exception)
    assert approved.stderr.strip() == "review not found", approved.stderr
    assert approved.stdout == "", approved.stdout
    assert reads == ["found"] * vanish_after_read + ["missing"], reads
    ledger = runs_root / "runs" / "default" / "ledger.jsonl"
    assert not ledger.exists() or list(HashChainLedger(ledger).entries()) == []


def test_facts_list_cases_show_proposals_queue(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from contracts.fact import Fact, FactProvenance
    from recertia.memory.episodic import CaseRecord, EpisodicStore
    from recertia.memory.semantic import FactStore
    from recertia.proposals.store import ProposalRecord, ProposalStore

    runs_root = tmp_path / "root"
    tenant = "default"
    facts = FactStore(runs_root / "runs" / tenant / "facts")
    facts.write(
        Fact(
            fact_id="uses-python",
            scope="project",
            slug="uses-python",
            assertion="This repository uses Python 3.12",
            provenance=FactProvenance(asserting_human="test"),
            authored_at=datetime.now(timezone.utc),
        )
    )
    listed = runner.invoke(
        app,
        ["facts", "list", "--scope", "project", "--runs-root", str(runs_root), "--tenant", tenant],
    )
    assert listed.exit_code == 0, listed.output
    assert "uses-python" in listed.output

    cases = EpisodicStore(runs_root / "runs" / tenant / "episodic")
    cases.write(
        CaseRecord(
            case_id="case-demo",
            run_id="run-1",
            attempt_no=1,
            outcome="solved",
            request_excerpt="hello world",
        )
    )
    shown = runner.invoke(
        app,
        ["cases", "show", "case-demo", "--runs-root", str(runs_root), "--tenant", tenant],
    )
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["case_id"] == "case-demo"

    store = ProposalStore(runs_root / "proposals.sqlite")
    store.add(
        ProposalRecord(
            proposal_id="p1",
            kind="policy",
            skill_id="policy",
            version=0,
            rationale="t2",
            tenant_id=tenant,
        )
    )
    store.close()
    queue = runner.invoke(
        app, ["proposals", "queue", "--runs-root", str(runs_root), "--tenant", tenant]
    )
    assert queue.exit_code == 0, queue.output
    assert "p1" in queue.output
