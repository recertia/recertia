"""Characterization: success / import / paper share one candidate-hygiene owner."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.criteria import SkillCertificationCriterion
from contracts.skill import Hygiene, Provenance, SkillVersion, Step
from recertia.distill.candidate import DistillRejected, assert_candidate_hygiene
from recertia.distill.paper import distill_paper
from recertia.distill.success import distill_success
from recertia.jobs.arxiv import ArxivPaper


def _skill(*, command: str = "true", run: str = "true") -> SkillVersion:
    now = datetime.now(timezone.utc)
    return SkillVersion(
        skill_id="hygiene-probe",
        version=1,
        title="Hygiene probe skill title",
        intent="Probe shared candidate hygiene gates for true-noop refusal",
        task_class="repo-chore",
        steps=[
            Step(
                id="s1",
                tool="shell",
                intent="Maybe a no-op",
                inputs={"command": command},
            )
        ],
        certification_criteria=[
            SkillCertificationCriterion(
                id="c1",
                kind="command",
                run=run,
                authored_by="distiller",
                weight=1.0,
                preregistered=True,
            )
        ],
        provenance=Provenance(distilled_from_run="hygiene-probe", distilled_at=now),
        hygiene=Hygiene(secret_scan="passed", scanned_at=now),
    )


def test_assert_candidate_hygiene_refuses_true_noop() -> None:
    with pytest.raises(DistillRejected, match="shell steps|command criterion"):
        assert_candidate_hygiene(_skill(command="true", run="test -f x"))
    with pytest.raises(DistillRejected, match="shell steps|command criterion"):
        assert_candidate_hygiene(_skill(command="printf hi > x", run="true"))


def test_assert_candidate_hygiene_accepts_replayable() -> None:
    cleaned = assert_candidate_hygiene(
        _skill(command="printf hi > x", run="test -f x")
    )
    assert cleaned.hygiene.secret_scan == "passed"


def test_success_distill_refuses_true_placeholder(tmp_path: Path) -> None:
    from contracts.run import RunState, Task

    workdir = tmp_path / "w"
    workdir.mkdir()
    state = RunState(
        run_id="noop-run",
        task=Task(
            task_id="t",
            request="do nothing",
            task_class="repo-chore",
            submitted_at=datetime.now(timezone.utc),
        ),
    )
    draft, _facts, verdict = distill_success(
        state, workdir=workdir, commands=["true"]
    )
    assert draft is None
    assert verdict.verdict == "one_off"
    assert "true-noop" in verdict.reason or "replayable" in verdict.reason


def test_paper_distill_stamps_shared_hygiene() -> None:
    paper = ArxivPaper(
        arxiv_id="2605.22148",
        title="Ratchet: A Minimal Hygiene Recipe for Self-Evolving LLM Agents",
        abstract=(
            "Lifecycle management of skill libraries is largely neglected. "
            "We show that bounded active caps recover gains. "
            "Without a finite cap the bound collapses."
        ),
        authors=("A",),
        categories=("cs.AI",),
    )
    skill, _facts = distill_paper(paper)
    assert skill.hygiene.secret_scan == "passed"
    assert skill.steps
    assert all(s.inputs.get("command") != "true" for s in skill.steps)
