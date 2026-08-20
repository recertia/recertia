"""Offline tests for paper distill + arxiv-keyed facts."""

from __future__ import annotations

from pathlib import Path

from recertia.distill.paper import distill_paper, facts_from_paper
from recertia.jobs.arxiv import ArxivPaper
from recertia.jobs.paper_pipeline import paper_from_payload, submit_paper_proposals
from recertia.jobs import Proposal
from recertia.memory.procedural.store import SkillStore
from recertia.memory.semantic import FactStore


def _ratchet() -> ArxivPaper:
    return ArxivPaper(
        arxiv_id="2605.22148",
        title="Ratchet: A Minimal Hygiene Recipe for Self-Evolving LLM Agents",
        abstract=(
            "Lifecycle management of skill libraries is largely neglected. "
            "We show that bounded active caps and contribution-based retirement recover gains. "
            "Without a finite cap the bound collapses. A biased judge can silently disable retirement. "
            "We propose a minimal hygiene recipe that lifts held-out pass rates."
        ),
        authors=("A", "B"),
        categories=("cs.AI", "cs.LG"),
        abs_url="https://arxiv.org/abs/2605.22148",
        pdf_url="https://arxiv.org/pdf/2605.22148.pdf",
    )


def test_distill_paper_pitfalls_and_claims() -> None:
    skill, facts = distill_paper(_ratchet())
    assert skill.provenance.curation == "mined_from_paper"
    assert skill.task_class == "research-synthesis"
    assert skill.failure_modes, "expected pitfall modes from abstract cues"
    assert skill.steps, "expected claim steps"
    assert any(f.slug.startswith("arxiv-2605-22148") for f in facts)
    assert any("pitfall" in f.slug for f in facts)


def test_facts_from_paper_keys() -> None:
    facts = facts_from_paper(_ratchet(), claims=["We show lift."], pitfalls=["Without a finite cap."])
    ids = {f.fact_id for f in facts}
    assert any(i.startswith("arxiv-2605-22148-claim") for i in ids)
    assert any(i.startswith("arxiv-2605-22148-pitfall") for i in ids)


def test_submit_paper_writes_candidate_and_facts(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    facts = FactStore(tmp_path / "facts")
    paper = _ratchet()
    proposal = Proposal(
        kind="mine",
        skill_id=paper.skill_id_slug(),
        version=1,
        rationale="mined_from_paper",
        payload={
            "curation": "mined_from_paper",
            "arxiv_id": paper.arxiv_id,
            "title": paper.title,
            "abstract": paper.abstract,
            "abs_url": paper.abs_url,
            "task_class": "research-synthesis",
        },
    )
    results = submit_paper_proposals(store, [proposal], fact_store=facts, distill=True)
    assert len(results) == 1
    draft, written_facts = results[0]
    assert draft.skill_id.startswith("arxiv-")
    assert written_facts
    listed = facts.list_facts()
    assert any("2605-22148" in f.slug for f in listed)


def test_paper_from_payload_roundtrip() -> None:
    p = paper_from_payload(
        {
            "arxiv_id": "2605.22148",
            "title": "Ratchet title long enough",
            "abstract": "body",
            "authors": ["A"],
            "categories": ["cs.AI"],
        }
    )
    assert p.arxiv_id == "2605.22148"
    assert p.skill_id_slug().startswith("arxiv-")
