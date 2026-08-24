"""Paper mine → optional PDF → distill → candidate skill + FactStore.

Improvement-plane only. Never writes approved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from contracts.fact import Fact
from contracts.skill import SkillVersion
from recertia.distill.paper import distill_paper
from recertia.jobs import Proposal
from recertia.jobs.arxiv import ArxivPaper
from recertia.jobs.arxiv_pdf import PdfExtractResult, fetch_and_extract
from recertia.memory.procedural.store import SkillStore
from recertia.memory.semantic import FactStore


def paper_from_payload(payload: dict) -> ArxivPaper:
    return ArxivPaper(
        arxiv_id=str(payload.get("arxiv_id") or ""),
        title=str(payload.get("title") or "untitled paper xx"),
        abstract=str(payload.get("abstract") or ""),
        authors=tuple(payload.get("authors") or ()),
        categories=tuple(payload.get("categories") or ()),
        published=payload.get("published"),
        updated=payload.get("updated"),
        pdf_url=payload.get("pdf_url"),
        abs_url=payload.get("abs_url"),
        primary_category=payload.get("primary_category"),
        comment=payload.get("comment"),
    )


def enrich_proposals_with_pdf(
    proposals: list[Proposal],
    *,
    dest_dir: Path,
    use_sandbox: bool = False,
    sandbox_workdir: Path | None = None,
) -> list[Proposal]:
    """Download PDFs for paper proposals; attach extract metadata on the payload."""

    out: list[Proposal] = []
    for proposal in proposals:
        payload = dict(proposal.payload or {})
        if payload.get("curation") != "mined_from_paper" and "arxiv_id" not in payload:
            out.append(proposal)
            continue
        paper = paper_from_payload(payload)
        if not paper.arxiv_id:
            out.append(proposal)
            continue
        result: PdfExtractResult = fetch_and_extract(
            paper,
            dest_dir,
            extract=True,
            use_sandbox=use_sandbox,
            sandbox_workdir=sandbox_workdir,
        )
        payload["pdf_path"] = str(result.pdf_path) if result.pdf_path else None
        payload["pdf_extract_method"] = result.method
        payload["pdf_extract_chars"] = result.chars
        if result.text:
            payload["pdf_text_preview"] = result.text[:2000]
            payload["_pdf_text"] = result.text
        out.append(
            Proposal(
                kind=proposal.kind,
                skill_id=proposal.skill_id,
                version=proposal.version,
                rationale=proposal.rationale,
                payload=payload,
                created_at=proposal.created_at,
            )
        )
    return out


def submit_paper_proposals(
    store: SkillStore,
    proposals: Iterable[Proposal],
    *,
    fact_store: FactStore | None = None,
    distill: bool = True,
) -> list[tuple[SkillVersion, list[Fact]]]:
    """Write candidate skills (+ optional facts). Never approved."""

    results: list[tuple[SkillVersion, list[Fact]]] = []
    for proposal in proposals:
        payload = dict(proposal.payload or {})
        is_paper = payload.get("curation") == "mined_from_paper" or "arxiv_id" in payload
        if not is_paper:
            from recertia.jobs.workers import enqueue_mined_candidate

            draft = enqueue_mined_candidate(store, proposal)
            results.append((draft, []))
            continue

        paper = paper_from_payload(payload)
        pdf_text = payload.pop("_pdf_text", None) or None
        if distill:
            draft, facts = distill_paper(
                paper,
                task_class=str(payload.get("task_class") or "research-synthesis"),
                pdf_text=pdf_text,
            )
        else:
            from recertia.jobs.workers import draft_from_mine_proposal

            draft = draft_from_mine_proposal(proposal)
            facts = []
            if fact_store is not None:
                from recertia.distill.paper import facts_from_paper

                facts = facts_from_paper(paper, pdf_text=pdf_text)

        written = store.write_candidate(draft)
        if fact_store is not None:
            for fact in facts:
                fact_store.write(fact)
        results.append((written, facts))
    return results
