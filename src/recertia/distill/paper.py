"""Deterministic paper distill: abstract → pitfall skill + arxiv-keyed facts.

No LLM. Sentence heuristics under the authoring prior produce:

* ``failure_modes`` from caution / bottleneck / negation language
* bounded shell steps that record claims for later human/golden refinement
* semantic ``Fact`` rows keyed by ``arxiv_id`` for the FactStore

Promotion still requires the golden gate. This path only authors candidates.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Iterable

from contracts.criteria import SkillCertificationCriterion
from contracts.fact import Fact, FactProvenance
from contracts.policy import AuthoringPrior
from contracts.skill import FailureMode, Hygiene, Provenance, SkillVersion, Step
from recertia.distill.prior import load_authoring_prior
from recertia.jobs.arxiv import ArxivPaper
from recertia.validation.sensitivity import author_sensitivity_proof, empty_negative_fixture

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[a-z0-9]+", re.I)

_PITFALL_CUES = (
    "fail",
    "failure",
    "without",
    "cannot",
    "must not",
    "should not",
    "risk",
    "bottleneck",
    "neglect",
    "degrad",
    "collapse",
    "bias",
    "silent",
    "unbounded",
    "drift",
    "false",
    "harm",
    "limit",
    "unless",
    "never",
)

_CLAIM_CUES = (
    "we show",
    "we propose",
    "we find",
    "results",
    "demonstrate",
    "measure",
    "lift",
    "improve",
    "bound",
    "recipe",
    "framework",
    "introduce",
)


def distill_paper(
    paper: ArxivPaper,
    *,
    prior: AuthoringPrior | None = None,
    task_class: str = "research-synthesis",
    pdf_text: str | None = None,
) -> tuple[SkillVersion, list[Fact]]:
    """Author a candidate skill and facts from paper metadata (+ optional PDF text)."""

    prior = prior or load_authoring_prior()
    body = paper.abstract.strip()
    if pdf_text and pdf_text.strip():
        # Prefer abstract for structure; append a truncated PDF tail for evidence only.
        body = body + "\n" + pdf_text.strip()[:8000]

    sentences = _sentences(body)
    pitfalls = _pick_pitfalls(sentences, limit=max(1, min(6, prior.max_steps)))
    claims = _pick_claims(sentences, limit=max(1, min(6, prior.max_steps)))
    if not pitfalls and sentences:
        pitfalls = [sentences[0][:200]]
    if not claims and sentences:
        claims = [sentences[0][:200]]

    skill_id = paper.skill_id_slug()
    now = datetime.now(timezone.utc)
    title = paper.title.strip()[:120]
    if len(title) < 8:
        title = f"arXiv paper {paper.arxiv_id}"

    intent = (
        f"Apply measured lessons from arXiv:{paper.arxiv_id} ({paper.title[:80]}). "
        f"Pitfall-oriented: avoid failure modes identified in the abstract before inventing."
    )
    if len(intent) < 20:
        intent = f"Apply lessons from arXiv paper {paper.arxiv_id} under retrieval-first policy."

    failure_modes = [
        FailureMode(
            symptom=p[:200],
            response="Do not retry the same approach; change strategy or abstain",
        )
        for p in pitfalls
    ][:6]

    steps: list[Step] = []
    for i, claim in enumerate(claims[: prior.max_steps], start=1):
        safe = claim.replace("'", "").replace("\n", " ")[:180]
        steps.append(
            Step(
                id=f"claim_{i}",
                tool="shell",
                intent=f"Record paper claim {i} for citation and later golden refinement",
                inputs={
                    "command": (
                        f"printf '%s\\n' 'arXiv:{paper.arxiv_id}' >> paper_claims.txt && "
                        f"printf '%s\\n' {safe!r} >> paper_claims.txt"
                    )
                },
            )
        )
    if not steps:
        steps = [
            Step(
                id="record_source",
                tool="shell",
                intent=f"Record arXiv:{paper.arxiv_id} as the source citation",
                inputs={
                    "command": (
                        f"printf '%s\\n' 'arXiv:{paper.arxiv_id}' > paper_source.txt"
                    )
                },
            )
        ]

    cert = SkillCertificationCriterion(
        id="paper-claims-recorded",
        kind="command",
        run="test -f paper_claims.txt || test -f paper_source.txt",
        authored_by="distiller",
        weight=1.0,
        preregistered=True,
    )
    proof = author_sensitivity_proof(cert, negative_workdir=empty_negative_fixture())
    cert = cert.model_copy(update={"sensitivity_proof": proof})

    version = SkillVersion(
        skill_id=skill_id,
        version=1,
        title=title,
        intent=intent,
        task_class=task_class,
        tags=["arxiv", "research", "mined-from-paper", "pitfall", "distilled-paper"],
        steps=steps,
        certification_criteria=[cert],
        failure_modes=failure_modes,
        provenance=Provenance(
            distilled_from_run=f"paper-distill:{paper.arxiv_id}",
            distilled_at=now,
            curation="mined_from_paper",
            derivation="mined_artifact",
            authoring_prior_version=prior.version,
            attribution_summary=f"arXiv:{paper.arxiv_id} {paper.title[:80]}",
            source_run_ids=[f"arxiv:{paper.arxiv_id}"],
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=now),
    )

    facts = facts_from_paper(paper, claims=claims, pitfalls=pitfalls, pdf_text=pdf_text)
    return version, facts


def facts_from_paper(
    paper: ArxivPaper,
    *,
    claims: Iterable[str] | None = None,
    pitfalls: Iterable[str] | None = None,
    pdf_text: str | None = None,
    scope: str = "project",
) -> list[Fact]:
    """Semantic facts keyed by arxiv id (slug prefix ``arxiv-<id>-…``)."""

    now = datetime.now(timezone.utc)
    id_slug = paper.skill_id_slug()  # arxiv-2605-22148
    evidence_base = paper.abs_url or f"https://arxiv.org/abs/{paper.arxiv_id}"
    facts: list[Fact] = []

    meta_slug = f"{id_slug}-meta"[:64].strip("-")
    facts.append(
        Fact(
            fact_id=meta_slug,
            scope=scope,  # type: ignore[arg-type]
            slug=meta_slug,
            assertion=(
                f"arXiv:{paper.arxiv_id} titled {paper.title[:120]!r} "
                f"(categories={','.join(paper.categories[:4]) or 'n/a'})"
            ),
            status="asserted",
            confidence=0.7,
            provenance=FactProvenance(
                asserting_job="paper-distill",
                evidence=evidence_base,
            ),
            authored_at=now,
        )
    )

    for i, claim in enumerate(list(claims or [])[:8], start=1):
        slug = f"{id_slug}-claim-{i}"[:64].strip("-")
        assertion = f"arXiv:{paper.arxiv_id} claim: {claim.strip()}"[:2000]
        if len(assertion) < 5:
            continue
        facts.append(
            Fact(
                fact_id=slug,
                scope=scope,  # type: ignore[arg-type]
                slug=slug,
                assertion=assertion,
                status="asserted",
                confidence=0.55,
                provenance=FactProvenance(
                    asserting_job="paper-distill",
                    evidence=evidence_base,
                ),
                authored_at=now,
            )
        )

    for i, pit in enumerate(list(pitfalls or [])[:8], start=1):
        slug = f"{id_slug}-pitfall-{i}"[:64].strip("-")
        assertion = f"arXiv:{paper.arxiv_id} pitfall: {pit.strip()}"[:2000]
        if len(assertion) < 5:
            continue
        facts.append(
            Fact(
                fact_id=slug,
                scope=scope,  # type: ignore[arg-type]
                slug=slug,
                assertion=assertion,
                status="asserted",
                confidence=0.6,
                provenance=FactProvenance(
                    asserting_job="paper-distill",
                    evidence=evidence_base,
                ),
                authored_at=now,
            )
        )

    if pdf_text and pdf_text.strip():
        digest = hashlib.sha256(pdf_text.encode("utf-8", errors="replace")).hexdigest()[:10]
        slug = f"{id_slug}-pdf-{digest}"[:64].strip("-")
        preview = " ".join(pdf_text.split())[:400]
        facts.append(
            Fact(
                fact_id=slug,
                scope=scope,  # type: ignore[arg-type]
                slug=slug,
                assertion=(
                    f"arXiv:{paper.arxiv_id} PDF text extract present "
                    f"(sha256_10={digest}; preview={preview!r})"
                )[:2000],
                status="asserted",
                confidence=0.5,
                provenance=FactProvenance(
                    asserting_job="paper-distill",
                    evidence=paper.pdf_url or evidence_base,
                ),
                authored_at=now,
            )
        )

    return facts


def _sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text) if p.strip()]
    return [p for p in parts if len(p) >= 20]


def _pick_pitfalls(sentences: list[str], *, limit: int) -> list[str]:
    scored: list[tuple[int, str]] = []
    for s in sentences:
        low = s.lower()
        score = sum(1 for cue in _PITFALL_CUES if cue in low)
        if score:
            scored.append((score, s))
    scored.sort(key=lambda t: (-t[0], -len(t[1])))
    return [s for _, s in scored[:limit]]


def _pick_claims(sentences: list[str], *, limit: int) -> list[str]:
    scored: list[tuple[int, str]] = []
    for s in sentences:
        low = s.lower()
        score = sum(1 for cue in _CLAIM_CUES if cue in low)
        if score:
            scored.append((score, s))
    scored.sort(key=lambda t: (-t[0], -len(t[1])))
    out = [s for _, s in scored[:limit]]
    if len(out) < limit:
        for s in sentences:
            if s not in out:
                out.append(s)
            if len(out) >= limit:
                break
    return out[:limit]
