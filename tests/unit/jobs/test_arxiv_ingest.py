"""Offline tests for arXiv Atom parse and mined_from_paper proposals."""

from __future__ import annotations

from pathlib import Path

from recertia.jobs import Proposal
from recertia.jobs.arxiv import (
    ArxivPaper,
    normalize_arxiv_id,
    paper_to_payload,
    parse_atom_feed,
)
from recertia.jobs.workers import draft_from_mine_proposal, mine_from_arxiv
from recertia.memory.procedural.store import SkillStore

SAMPLE_ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2605.22148v1</id>
    <updated>2026-05-20T00:00:00Z</updated>
    <published>2026-05-20T00:00:00Z</published>
    <title>Ratchet: A Minimal Hygiene Recipe for Self-Evolving LLM Agents</title>
    <summary>
      Lifecycle management of skill libraries is largely neglected. We show that
      bounded active caps and contribution-based retirement recover large gains.
    </summary>
    <author><name>Test Author</name></author>
    <author><name>Second Author</name></author>
    <link href="https://arxiv.org/abs/2605.22148v1" rel="alternate" type="text/html"/>
    <link title="pdf" href="https://arxiv.org/pdf/2605.22148v1.pdf" rel="related" type="application/pdf"/>
    <arxiv:primary_category term="cs.AI"/>
    <category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
</feed>
"""


def test_normalize_arxiv_id() -> None:
    assert normalize_arxiv_id("2605.22148") == "2605.22148"
    assert normalize_arxiv_id("arXiv:2605.22148v1") == "2605.22148v1"
    assert normalize_arxiv_id("") == ""
    assert normalize_arxiv_id("not-an-id") == ""


def test_parse_atom_feed() -> None:
    papers = parse_atom_feed(SAMPLE_ATOM)
    assert len(papers) == 1
    p = papers[0]
    assert p.arxiv_id.startswith("2605.22148")
    assert "Ratchet" in p.title
    assert "Lifecycle management" in p.abstract
    assert p.authors == ("Test Author", "Second Author")
    assert "cs.AI" in p.categories
    assert p.pdf_url and "pdf" in p.pdf_url
    assert p.skill_id_slug().startswith("arxiv-2605-22148")


def test_paper_to_payload_curation() -> None:
    paper = ArxivPaper(
        arxiv_id="2605.22148",
        title="Ratchet test title long enough",
        abstract="Abstract body for payload test.",
        authors=("A",),
        categories=("cs.AI",),
    )
    payload = paper_to_payload(paper)
    assert payload["curation"] == "mined_from_paper"
    assert payload["arxiv_id"] == "2605.22148"


def test_mine_from_arxiv_with_stub_client(tmp_path: Path) -> None:
    class StubClient:
        def fetch_by_ids(self, ids):
            return [
                ArxivPaper(
                    arxiv_id="2605.22148",
                    title="Ratchet: hygiene for self-evolving agents",
                    abstract="Bounded caps and retirement recover gains.",
                    authors=("X",),
                    categories=("cs.AI",),
                    abs_url="https://arxiv.org/abs/2605.22148",
                    pdf_url="https://arxiv.org/pdf/2605.22148.pdf",
                )
            ]

        def search(self, query, **kwargs):
            return []

    store = SkillStore(tmp_path / "skills")
    proposals = mine_from_arxiv(
        store, arxiv_ids=["2605.22148"], client=StubClient()
    )
    assert len(proposals) == 1
    p = proposals[0]
    assert p.kind == "mine"
    assert p.payload["curation"] == "mined_from_paper"
    assert p.skill_id.startswith("arxiv-")


def test_draft_from_paper_proposal() -> None:
    proposal = Proposal(
        kind="mine",
        skill_id="arxiv-2605-22148",
        version=1,
        rationale="mined_from_paper: arXiv:2605.22148",
        payload={
            "curation": "mined_from_paper",
            "arxiv_id": "2605.22148",
            "title": "Ratchet: A Minimal Hygiene Recipe",
            "abstract": "Lifecycle management of skill libraries is largely neglected.",
            "abs_url": "https://arxiv.org/abs/2605.22148",
            "task_class": "research-synthesis",
        },
    )
    draft = draft_from_mine_proposal(proposal)
    assert draft.provenance.curation == "mined_from_paper"
    assert draft.task_class == "research-synthesis"
    assert draft.skill_id == "arxiv-2605-22148"
    assert any(t == "arxiv" for t in draft.tags)
