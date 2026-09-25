"""Assemble a federated ``MemoryBundle`` from plane-local reads.

Two call sites (the retrieve node and the debug query). Not a third retriever.
"""

from __future__ import annotations

from typing import Any, Protocol

from contracts.run import MemoryBundle, MemoryElementRef, SkillCandidateRef
from recertia.retrieval.config import RetrievalConfig


class _EpisodicPlane(Protocol):
    def dead_ends_for(self, *, task_class: str | None = None, limit: int = 3) -> list[Any]: ...

    def solved_case_ids_for(self, *, task_class: str | None = None, limit: int = 3) -> list[str]: ...


class _FactPlane(Protocol):
    def retrieve(self, query: str, *, scope: str = "project", limit: int = 10) -> list[Any]: ...


class _AffordancePlane(Protocol):
    tools: dict[str, Any]


def assemble_bundle(
    *,
    skills: list[SkillCandidateRef],
    query: str,
    task_class: str | None,
    episodic: _EpisodicPlane | None,
    facts: _FactPlane | None,
    affordances: _AffordancePlane | None,
    config: RetrievalConfig,
    suppressed: bool = False,
) -> MemoryBundle:
    """Stitch procedural hits with episodic / semantic / affordance citations."""

    if suppressed:
        return MemoryBundle(suppressed=True)

    dead_ends: list[MemoryElementRef] = []
    cases: list[MemoryElementRef] = []
    if episodic is not None:
        for case in episodic.dead_ends_for(task_class=task_class, limit=3):
            dead_ends.append(
                MemoryElementRef(
                    plane="episodic",
                    ref=case.case_id,
                    summary=(case.dead_end.why_failed if case.dead_end else case.outcome),
                )
            )
        for case_id in episodic.solved_case_ids_for(task_class=task_class, limit=3):
            cases.append(
                MemoryElementRef(plane="episodic", ref=case_id, summary="solved analogue")
            )

    tool_cautions: list[MemoryElementRef] = []
    if affordances is not None:
        for name, agg in affordances.tools.items():
            if (
                agg.flake_rate >= config.affordance_flake_rate
                and agg.invocations >= config.affordance_min_invocations
            ):
                tool_cautions.append(
                    MemoryElementRef(
                        plane="affordance",
                        ref=name,
                        summary=f"flake_rate={agg.flake_rate:.2f}",
                        trust=1.0 - agg.flake_rate,
                    )
                )

    fact_refs: list[MemoryElementRef] = []
    if facts is not None:
        for fact in facts.retrieve(query, limit=10):
            fact_refs.append(
                MemoryElementRef(
                    plane="semantic",
                    ref=fact.fact_id,
                    summary=fact.assertion[:200],
                    trust=fact.confidence,
                )
            )

    return MemoryBundle(
        skills=skills,
        facts=fact_refs,
        cases=cases,
        dead_ends=dead_ends,
        tool_cautions=tool_cautions,
    )
