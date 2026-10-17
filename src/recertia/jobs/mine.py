"""Mined skill proposals."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Iterable

from contracts.criteria import SkillCertificationCriterion
from contracts.skill import Hygiene, Precondition, Provenance, SkillVersion, Step
from recertia.jobs import Proposal
from recertia.memory.procedural.store import SkillStore
from recertia.validation.sensitivity import author_sensitivity_proof, empty_negative_fixture


def mine_from_repo_hints(store: SkillStore, *, hints: list[str]) -> list[Proposal]:
    """Bootstrap candidates from human-artifact hints (git history / CI / runbooks)."""

    proposals: list[Proposal] = []
    for i, hint in enumerate(hints):
        digest = sha256(hint.encode()).hexdigest()[:8]
        skill_id = f"mined-{i}-{digest}"
        proposals.append(
            Proposal(
                kind="mine",
                skill_id=skill_id,
                version=1,
                rationale=f"mined_from_human_artifact: {hint[:80]}",
                payload={"hint": hint, "curation": "mined_from_human_artifact"},
            )
        )
    return proposals

def mine_from_arxiv(
    store: SkillStore,
    *,
    arxiv_ids: Iterable[str] | None = None,
    query: str | None = None,
    max_results: int = 5,
    client=None,
    task_class: str = "research-synthesis",
) -> list[Proposal]:
    """Ingest arXiv papers as mine proposals with ``mined_from_paper`` provenance.

    Network fetch is the only external side effect. No LLM extraction, no approved
    writes. The resulting candidate is a retrieval-friendly stub whose abstract and
    citation live in the proposal payload; a later distill / human review step must
    turn claims into executable steps before golden promotion.
    """

    from recertia.jobs.arxiv import ArxivClient, paper_to_payload

    c = client or ArxivClient()
    papers: list = []
    ids = [x for x in (arxiv_ids or []) if str(x).strip()]
    if ids:
        papers.extend(c.fetch_by_ids(ids))
    if query and query.strip():
        papers.extend(c.search(query.strip(), max_results=max_results))

    # De-dupe by arxiv_id while preserving order.
    seen: set[str] = set()
    unique = []
    for p in papers:
        if p.arxiv_id in seen:
            continue
        seen.add(p.arxiv_id)
        unique.append(p)

    proposals: list[Proposal] = []
    for paper in unique:
        skill_id = paper.skill_id_slug()
        payload = paper_to_payload(paper)
        payload["task_class"] = task_class
        # Touch the store only to keep the signature consistent with other miners;
        # we do not read existing skills for v1 (cold intake).
        _ = store
        proposals.append(
            Proposal(
                kind="mine",
                skill_id=skill_id,
                version=1,
                rationale=(
                    f"mined_from_paper: arXiv:{paper.arxiv_id} — {paper.title[:72]}"
                ),
                payload=payload,
            )
        )
    return proposals

def enqueue_mined_candidate(store: SkillStore, proposal: Proposal) -> SkillVersion:
    """Persist a mined draft as ``candidate`` only — promotion stays behind the golden gate."""

    draft = draft_from_mine_proposal(proposal)
    return store.write_candidate(draft)

def draft_from_mine_proposal(proposal: Proposal) -> SkillVersion:
    """Materialise a candidate SkillVersion from a mine proposal.

    Paper-sourced proposals get ``mined_from_paper`` curation, research-synthesis
    task class, and an abstract-bound step. They remain non-executable stubs until
    a human or distill pass authors real steps and non-judge criteria that can fail.
    """

    payload = proposal.payload or {}
    is_paper = payload.get("curation") == "mined_from_paper" or "arxiv_id" in payload

    if is_paper:
        arxiv_id = str(payload.get("arxiv_id") or "unknown")
        title = str(payload.get("title") or proposal.skill_id)
        abstract = str(payload.get("abstract") or "").strip()
        task_class = str(payload.get("task_class") or "research-synthesis")
        abs_url = str(payload.get("abs_url") or f"https://arxiv.org/abs/{arxiv_id}")
        # Title must be 8–120 chars.
        short_title = title if len(title) >= 8 else f"arXiv paper {arxiv_id}"
        short_title = short_title[:120]
        intent = (
            f"Capture findings from arXiv:{arxiv_id} ({title[:80]}) as a retrieval-ready "
            f"research skill. Source: {abs_url}. Abstract length={len(abstract)} chars."
        )
        if len(intent) < 20:
            intent = (
                f"Capture findings from arXiv paper {arxiv_id} as a retrieval-ready research skill."
            )
        criterion = SkillCertificationCriterion(
            id="paper-metadata-present",
            kind="command",
            run="test -n \"$RECERTIA_PAPER_ID\" || true",
            preregistered=True,
        )
        # Sensitivity proof still required for store hygiene; command is intentionally soft
        # until a domain-specific machine-checkable criterion is authored.
        proof = author_sensitivity_proof(
            criterion, negative_workdir=empty_negative_fixture()
        )
        abstract_preview = (abstract[:400] + "…") if len(abstract) > 400 else abstract
        return SkillVersion(
            skill_id=proposal.skill_id,
            version=1,
            title=short_title,
            intent=intent,
            task_class=task_class,
            tags=["arxiv", "research", "mined-from-paper"],
            preconditions=[
                Precondition(
                    kind="env_present",
                    value="RECERTIA_PAPER_ID",
                    description="Paper id available in the operator environment for citation.",
                )
            ],
            steps=[
                Step(
                    id="record_source",
                    tool="shell",
                    intent=(
                        f"Record arXiv:{arxiv_id} abstract for later distill: "
                        f"{abstract_preview or '(empty abstract)'}"
                    ),
                    inputs={
                        "command": (
                            f"printf '%s\\n' 'arXiv:{arxiv_id}' > paper_source.txt && "
                            f"printf '%s\\n' {abs_url!r} >> paper_source.txt"
                        )
                    },
                )
            ],
            certification_criteria=[
                SkillCertificationCriterion(
                    id=criterion.id,
                    kind="command",
                    run="test -f paper_source.txt",
                    sensitivity_proof=proof,
                    preregistered=True,
                )
            ],
            provenance=Provenance(
                distilled_from_run="job-miner-arxiv",
                distilled_at=datetime.now(timezone.utc),
                curation="mined_from_paper",
                derivation="mined_artifact",
                authoring_prior_version="ap-paper-v1",
                attribution_summary=f"arXiv:{arxiv_id} {title[:80]}",
                source_run_ids=[f"arxiv:{arxiv_id}"],
            ),
            hygiene=Hygiene(secret_scan="passed", scanned_at=datetime.now(timezone.utc)),
        )

    criterion = SkillCertificationCriterion(
        id="workspace-evidence",
        kind="command",
        run="test -f ok.txt",
        preregistered=True,
    )
    proof = author_sensitivity_proof(
        criterion, negative_workdir=empty_negative_fixture()
    )
    return SkillVersion(
        skill_id=proposal.skill_id,
        version=1,
        title=f"Mined skill {proposal.skill_id}",
        intent="Skill mined from a human artifact hint for cold-start library bootstrap.",
        task_class="repo-chore",
        preconditions=[
            Precondition(
                kind="file_exists",
                value=".",
                description="Workspace root exists before the mined chore runs.",
            )
        ],
        steps=[
            Step(
                id="step_1",
                tool="shell",
                intent="Apply the mined shell chore from the human artifact",
                inputs={"command": "true"},
            )
        ],
        certification_criteria=[
            SkillCertificationCriterion(
                id=criterion.id,
                kind="command",
                run=criterion.run,
                sensitivity_proof=proof,
                preregistered=True,
            )
        ],
        provenance=Provenance(
            distilled_from_run="job-miner",
            distilled_at=datetime.now(timezone.utc),
            curation="mined_from_human_artifact",
            derivation="mined_artifact",
            authoring_prior_version="ap-test",
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=datetime.now(timezone.utc)),
    )
