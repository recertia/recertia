"""Procedural retrieval pipeline (specs §5).

Stages, in order: candidate generation → RRF merge → filter (preconditions, active set,
lifecycle, env fingerprint) → rerank → score floor → evidence/staleness/curation demotion →
top-3 return. Thin evidence is demoted, never hard-dropped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from contracts.run import MemoryBundle, SkillCandidateRef
from recertia.retrieval.config import RetrievalConfig
from recertia.retrieval.index import SkillIndex, cosine, embed_text, tokenize
from recertia.retrieval.preconditions import (
    environment_fingerprint_matches,
    evaluate_all,
    parse_preconditions_json,
)

BundleHook = Callable[[MemoryBundle], MemoryBundle]



@dataclass
class DropRecord:
    skill_id: str
    version: int
    stage: str
    reason: str


@dataclass
class RetrievalExplanation:
    """What ``recertia skills search --explain`` prints."""

    query: str
    snapshot_id: str
    lexical_hits: list[tuple[str, int, float]] = field(default_factory=list)
    vector_hits: list[tuple[str, int, float]] = field(default_factory=list)
    merged: list[tuple[str, int, float]] = field(default_factory=list)
    probe_evidence: dict[tuple[str, int], list[dict[str, object]]] = field(default_factory=dict)
    dropped: list[DropRecord] = field(default_factory=list)
    demoted: list[tuple[str, int, float, str]] = field(default_factory=list)
    returned: list[SkillCandidateRef] = field(default_factory=list)


def reciprocal_rank_fusion(
    ranked_lists: list[list[tuple[str, int, float]]],
    k: int = 60,
) -> list[tuple[str, int, float]]:
    """RRF over ``(skill_id, version, _)`` lists; returns ``(skill_id, version, rrf_score)``."""

    scores: dict[tuple[str, int], float] = {}
    for ranked in ranked_lists:
        for rank, (sid, ver, _) in enumerate(ranked, start=1):
            scores[(sid, ver)] = scores.get((sid, ver), 0.0) + 1.0 / (k + rank)
    return sorted(
        [(sid, ver, score) for (sid, ver), score in scores.items()],
        key=lambda t: t[2],
        reverse=True,
    )


class Retriever:
    def __init__(
        self,
        index: SkillIndex,
        config: RetrievalConfig | None = None,
        *,
        bundle_hook: BundleHook | None = None,
    ) -> None:
        self._index = index
        self.config = config or RetrievalConfig()
        # Eval-only. Production callers (bootstrap, retrieve node, CLI search) omit this.
        # Constructor-only; not on RetrievalConfig so a policy flag cannot turn it on.
        self._bundle_hook = bundle_hook
        from recertia.retrieval.cache import RetrievalCache

        self.result_cache = RetrievalCache()

    @property
    def bundle_hook(self) -> BundleHook | None:
        return self._bundle_hook

    @property
    def index(self) -> SkillIndex:
        """Backing index for operators/CLI; task-plane code should use :meth:`rebuild`."""

        return self._index

    def rebuild(self, entries: list[tuple], *, library_fingerprint: str | None = None) -> str:
        """Rebuild the retrieval index from loaded skill rows (store-node hook)."""

        snapshot_id = self._index.rebuild(entries, library_fingerprint=library_fingerprint)  # type: ignore[arg-type]
        self.result_cache.invalidate_all()
        return snapshot_id

    def upsert(self, version, status, stats, *, library_fingerprint: str | None = None) -> str:
        """Incrementally index one skill version (store-node hook)."""

        prior = self._index.snapshot_id()
        snapshot_id = self._index.upsert(
            version, status, stats, library_fingerprint=library_fingerprint
        )
        if prior:
            self.result_cache.invalidate_snapshot(prior)
        return snapshot_id

    def is_fresh(self, library_fingerprint: str) -> bool:
        """Whether the index matches a library with exactly this fingerprint."""

        return self._index.is_fresh(library_fingerprint)

    def snapshot_id(self) -> str:
        """Current library index snapshot id without exposing the backing index."""

        return self._index.snapshot_id()

    def search(
        self,
        query: str,
        *,
        workdir: Path,
        env_fingerprint: dict[str, str] | None = None,
        readable_scopes: set[str] | None = None,
        suppress: bool = False,
    ) -> tuple[MemoryBundle, RetrievalExplanation]:
        cfg = self.config
        env_fingerprint = env_fingerprint or {}
        readable_scopes = readable_scopes or {"run", "project", "org", "global"}
        explanation = RetrievalExplanation(query=query, snapshot_id=self._index.snapshot_id())

        if suppress:
            return MemoryBundle(suppressed=True), explanation

        from recertia.ops.systems import canonical_args_hash
        from recertia.telemetry import emit_in_run

        cached = self.result_cache.lookup(
            query, snapshot_id=explanation.snapshot_id, env_fingerprint=env_fingerprint
        )
        if cached is not None:
            emit_in_run(
                "retrieve.queried",
                cache="hit",
                canonical_key=f"{explanation.snapshot_id}:{canonical_args_hash({'q': query})}",
            )
            emit_in_run("cache.hit", kind="retrieve")
            return cached

        q_emb, lexical, vector = self._generate_candidates(query)
        explanation.lexical_hits = lexical
        explanation.vector_hits = vector

        merged = reciprocal_rank_fusion([lexical, vector], k=cfg.rrf_k)
        explanation.merged = merged

        survivors = self._filter_merged(
            merged, workdir, env_fingerprint, readable_scopes, explanation
        )
        reranked = self._rerank(survivors, query, q_emb)
        floored = self._apply_score_floor(reranked, explanation)
        final = self._apply_demotions(floored, explanation)
        candidates = self._top_candidates(final, lexical, vector)

        explanation.returned = candidates
        bundle = MemoryBundle(skills=candidates)
        if self._bundle_hook is not None:
            bundle = self._bundle_hook(bundle)
        self.result_cache.store(
            query,
            bundle,
            explanation,
            snapshot_id=explanation.snapshot_id,
            env_fingerprint=env_fingerprint,
        )
        emit_in_run(
            "retrieve.queried",
            cache="miss",
            canonical_key=f"{explanation.snapshot_id}:{canonical_args_hash({'q': query})}",
        )
        emit_in_run("cache.miss", kind="retrieve")
        return bundle, explanation


    def _generate_candidates(
        self, query: str
    ) -> tuple[list[float], list[tuple[str, int, float]], list[tuple[str, int, float]]]:
        """One query embedding shared by the vector scan and the rerank stage."""

        cfg = self.config
        q_emb = embed_text(query)
        lexical = self._index.lexical_top_k(query, cfg.lexical_top_k)
        vector = self._index.vector_top_k(query, cfg.vector_top_k, q_emb=q_emb)
        return q_emb, lexical, vector

    def _filter_merged(
        self,
        merged: list[tuple[str, int, float]],
        workdir: Path,
        env_fingerprint: dict[str, str],
        readable_scopes: set[str],
        explanation: RetrievalExplanation,
    ) -> list[tuple[str, int, float, dict]]:
        fetched = self._index.get_rows((sid, ver) for sid, ver, _ in merged)
        survivors: list[tuple[str, int, float, dict]] = []
        for sid, ver, rrf_score in merged:
            row = fetched.get((sid, ver))
            if row is None:
                continue
            drop = self._filter_row(row, workdir, env_fingerprint, readable_scopes, explanation)
            if drop is not None:
                explanation.dropped.append(drop)
                continue
            survivors.append((sid, ver, rrf_score, row))
        return survivors

    def _rerank(
        self,
        survivors: list[tuple[str, int, float, dict]],
        query: str,
        q_emb: list[float],
    ) -> list[tuple[str, int, float, dict]]:
        """Rerank top N by cosine + lexical overlap. Thin tail keeps a capped RRF score."""

        cfg = self.config
        reranked: list[tuple[str, int, float, dict]] = []
        for sid, ver, rrf, row in survivors[: cfg.rerank_top_n]:
            doc_emb = self._index.embedding_for(sid, ver)
            if doc_emb is None:
                doc_emb = tuple(embed_text(row["document"]))
            vec = cosine(q_emb, doc_emb)
            overlap = _lexical_overlap(query, row["document"])
            # Prefer skills whose id tokens appear in the query (strong chore-label signal).
            id_boost = 0.15 if _id_tokens_in_query(sid, query) else 0.0
            score = 0.35 * vec + 0.50 * overlap + id_boost + 0.15 * min(rrf * 20.0, 1.0)
            reranked.append((sid, ver, score, row))
        for sid, ver, rrf, row in survivors[cfg.rerank_top_n :]:
            reranked.append((sid, ver, min(rrf * 20.0, cfg.min_score), row))
        reranked.sort(key=lambda t: t[2], reverse=True)
        return reranked

    def _apply_score_floor(
        self,
        reranked: list[tuple[str, int, float, dict]],
        explanation: RetrievalExplanation,
    ) -> list[tuple[str, int, float, dict]]:
        cfg = self.config
        floored: list[tuple[str, int, float, dict]] = []
        for sid, ver, score, row in reranked:
            if score < cfg.min_score:
                explanation.dropped.append(
                    DropRecord(sid, ver, "score_floor", f"score={score:.3f}<{cfg.min_score}")
                )
                continue
            floored.append((sid, ver, score, row))
        return floored

    def _apply_demotions(
        self,
        floored: list[tuple[str, int, float, dict]],
        explanation: RetrievalExplanation,
    ) -> list[tuple[str, int, float, dict]]:
        final: list[tuple[str, int, float, dict]] = []
        for sid, ver, score, row in floored:
            demoted_score, demote_reason = self._demote(score, row)
            if demote_reason:
                explanation.demoted.append((sid, ver, demoted_score, demote_reason))
            final.append((sid, ver, demoted_score, row))
        final.sort(key=lambda t: t[2], reverse=True)
        return final

    def _top_candidates(
        self,
        final: list[tuple[str, int, float, dict]],
        lexical: list[tuple[str, int, float]],
        vector: list[tuple[str, int, float]],
    ) -> list[SkillCandidateRef]:
        lexical_ranks = {(sid, ver): i for i, (sid, ver, _) in enumerate(lexical, start=1)}
        vector_ranks = {(sid, ver): i for i, (sid, ver, _) in enumerate(vector, start=1)}
        candidates: list[SkillCandidateRef] = []
        for sid, ver, score, _row in final[: self.config.max_candidates]:
            candidates.append(
                SkillCandidateRef(
                    skill_id=sid,
                    version=ver,
                    score=round(score, 4),
                    lexical_rank=lexical_ranks.get((sid, ver)),
                    vector_rank=vector_ranks.get((sid, ver)),
                )
            )
        return candidates


    def _filter_row(
        self,
        row: dict,
        workdir: Path,
        env_fingerprint: dict[str, str],
        readable_scopes: set[str],
        explanation: RetrievalExplanation,
    ) -> DropRecord | None:
        sid, ver = row["skill_id"], int(row["version"])
        # Shadow evidence is collected by the dedicated shadow runner.  It
        # must never be offered as an online caller-visible candidate.
        if row["lifecycle"] != "approved":
            return DropRecord(sid, ver, "lifecycle", f"lifecycle={row['lifecycle']}")
        # Approved skills must be in the bounded active set to apply.
        if not row["active"]:
            return DropRecord(sid, ver, "active_set", "approved but active=False")
        if row["scope"] not in readable_scopes:
            return DropRecord(sid, ver, "scope", f"scope={row['scope']} not readable")

        skill_fp = json.loads(row["tool_fingerprint_json"])
        ok, reason = environment_fingerprint_matches(skill_fp, env_fingerprint)
        if not ok:
            return DropRecord(sid, ver, "env_fingerprint", reason)

        preconditions = parse_preconditions_json(row["preconditions_json"])
        ok, evidence = evaluate_all(preconditions, workdir, budget_units=self.config.probe_budget_units)
        explanation.probe_evidence[(sid, ver)] = [
            {
                "probe": item.probe,
                "passed": item.passed,
                "detail": item.detail,
                "cost_units": item.cost_units,
            }
            for item in evidence
        ]
        if not ok:
            return DropRecord(
                sid, ver, "precondition", evidence[-1].reason if evidence else "failed"
            )
        return None

    def _demote(self, score: float, row: dict) -> tuple[float, str | None]:
        cfg = self.config
        reasons: list[str] = []
        applications = int(row["applications"])
        if applications < cfg.evidence_floor:
            score *= cfg.low_evidence_factor
            reasons.append(f"thin_evidence:applications={applications}<{cfg.evidence_floor}")

        curation = row["curation"]
        if curation == "human_authored":
            score *= cfg.human_authored_prior
        elif curation == "mined_from_human_artifact":
            score *= cfg.mined_prior
            reasons.append("curation_prior:mined")
        else:
            score *= cfg.self_distilled_prior
            reasons.append("curation_prior:self_distilled")

        last_used = row["last_used_at"]
        if last_used:
            try:
                ts = datetime.fromisoformat(last_used)
                age_days = (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
                if age_days > 0:
                    decay = 0.5 ** (age_days / cfg.staleness_half_life_days)
                    score *= decay
                    if decay < 0.99:
                        reasons.append(f"staleness:age_days={age_days:.1f},factor={decay:.3f}")
            except ValueError:
                pass

        return score, "; ".join(reasons) if reasons else None


def _lexical_overlap(query: str, document: str) -> float:
    q = set(tokenize(query))
    d = set(tokenize(document))
    if not q:
        return 0.0
    return len(q & d) / len(q)


def _id_tokens_in_query(skill_id: str, query: str) -> bool:
    id_tokens = set(skill_id.split("-"))
    q_tokens = set(tokenize(query))
    # Require at least half the id tokens to appear (avoids boosting on a lone "add").
    if not id_tokens:
        return False
    return len(id_tokens & q_tokens) >= max(2, (len(id_tokens) + 1) // 2)
