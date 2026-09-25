"""Federated retrieve debug across procedural / semantic / episodic planes (RW-SUR).

Read-only: a stale index is refused, never rebuilt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from recertia.memory.affordance import AffordanceStore
from recertia.memory.episodic import EpisodicStore
from recertia.memory.procedural.store import SkillStore
from recertia.memory.semantic import FactStore
from recertia.retrieval.bundle import assemble_bundle
from recertia.retrieval.config import RetrievalConfig
from recertia.retrieval.index import SkillIndex
from recertia.retrieval.pipeline import Retriever


class IndexStaleError(RuntimeError):
    """Debug query refused because the on-disk index does not match the library."""


def federated_query(
    query: str,
    *,
    skills_root: Path | str,
    facts_root: Path | str,
    episodic_root: Path | str,
    index_path: Path | str,
    workdir: Path | str,
    env_fingerprint: dict[str, str] | None = None,
    limit: int = 8,
    affordance_path: Path | str | None = None,
) -> dict[str, Any]:
    """Score + drop reasons across planes. Does not start a run. Does not mutate the index."""

    store = SkillStore(skills_root)
    index = SkillIndex(index_path)
    try:
        fingerprint = store.library_fingerprint()
        if not index.is_fresh(fingerprint):
            return {
                "query": query,
                "error": "index_stale",
                "snapshot_id": index.snapshot_id(),
                "skills": {"returned": [], "dropped": [], "demoted": []},
                "facts": [],
                "cases": [],
                "affordances": {"tools": [], "resources": []},
            }
        retriever = Retriever(index)
        bundle, explanation = retriever.search(
            query,
            workdir=Path(workdir),
            env_fingerprint=env_fingerprint or {},
        )
    finally:
        index.close()

    facts_store = FactStore(facts_root)
    episodic = EpisodicStore(episodic_root)
    aff = AffordanceStore(affordance_path) if affordance_path is not None else None
    assembled = assemble_bundle(
        skills=list(bundle.skills),
        query=query,
        task_class=None,
        episodic=episodic,
        facts=facts_store,
        affordances=aff,
        config=retriever.config if "retriever" in locals() else RetrievalConfig(),
    )
    affordances: dict[str, Any] = {"tools": [], "resources": []}
    if aff is not None:
        affordances = {
            "tools": [
                {
                    "tool": t.tool,
                    "invocations": t.invocations,
                    "failure_rate": t.failure_rate,
                }
                for t in aff.tools.values()
            ],
            "resources": [
                {"kind": r.kind, "id": r.id, "conflicts": r.conflicts}
                for r in aff.resources.values()
            ],
        }
    return {
        "query": query,
        "snapshot_id": explanation.snapshot_id,
        "skills": {
            "returned": [
                {"skill_id": c.skill_id, "version": c.version, "score": c.score}
                for c in assembled.skills
            ],
            "dropped": [
                {
                    "skill_id": d.skill_id,
                    "version": d.version,
                    "stage": d.stage,
                    "reason": d.reason,
                }
                for d in explanation.dropped
            ],
            "demoted": [
                {
                    "skill_id": sid,
                    "version": ver,
                    "score": score,
                    "reason": reason,
                }
                for sid, ver, score, reason in explanation.demoted
            ],
        },
        "facts": [f.model_dump(mode="json") for f in facts_store.retrieve(query, limit=limit)],
        "cases": episodic.list_index()[-limit:],
        "affordances": affordances,
        "bundle_citations": {
            "dead_ends": len(assembled.dead_ends),
            "cases": len(assembled.cases),
            "tool_cautions": len(assembled.tool_cautions),
            "facts": len(assembled.facts),
        },
    }
