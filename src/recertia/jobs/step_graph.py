"""Step-graph proposals: parallelise, serialise, hex, compress, corrections, shadow."""

from __future__ import annotations

import json
from pathlib import Path

from contracts.skill import SkillVersion
from recertia.evals.store import EvalStore
from recertia.jobs import Proposal
from recertia.memory.procedural.store import SkillStore
from recertia.review.autonomy_config import DEFAULT_AUTONOMY, AutonomyConfig


def propose_hex_search() -> list[Proposal]:
    """Emit a HEX practice-search proposal. JobRunner still gates enablement (RW-6)."""

    return [
        Proposal(
            kind="hex",
            skill_id="hex-search",
            version=1,
            rationale="HEX practice search (enablement predicates passed)",
            payload={"job": "practice_hex"},
        )
    ]

def propose_compress() -> list[Proposal]:
    """Emit a unit-level compress proposal. JobRunner still gates enablement (RW-6)."""

    return [
        Proposal(
            kind="compress",
            skill_id="compress-units",
            version=1,
            rationale="unit-level compress (enablement predicates passed)",
            payload={"job": "compress"},
        )
    ]

def schedule_shadow_evaluations(
    store: SkillStore,
    *,
    eval_store: EvalStore | None = None,
    config: AutonomyConfig | None = None,
    snapshot_id: str = "shadow-job",
) -> list[Proposal]:
    """Job wrapper: bounded offline shadow slots with no caller-visible side effects."""

    from recertia.review.shadow import schedule_shadow_slots

    cfg = config or DEFAULT_AUTONOMY
    results = schedule_shadow_slots(
        store, eval_store=eval_store, config=cfg, snapshot_id=snapshot_id
    )
    return [
        Proposal(
            kind="curate",
            skill_id=result.skill_id,
            version=result.version,
            rationale=(
                f"offline shadow slot {'passed' if result.success else 'failed'}; "
                "not visible to callers"
            ),
            payload={
                "shadow": True,
                "success": result.success,
                "visible_to_caller": result.visible_to_caller,
                "run_id": result.run_id,
            },
        )
        for result in results
    ]

def propose_parallelise(
    skill_id: str,
    version: int,
    *,
    fake_edge_failures: int | None = None,
    skill: SkillVersion | None = None,
    transcripts: list[dict] | None = None,
    threshold: int = 5,
) -> list[Proposal]:
    """Propose removing input bindings that failed the fake-edge test (≥``threshold`` runs).

    Prefer deriving failure counts from ``skill`` + historical ``transcripts`` when supplied;
    otherwise use an explicit ``fake_edge_failures`` total.
    """

    from recertia.evals.fake_edges import edges_failing_threshold, fake_edge_failure_count

    remove: list[dict[str, str]] = []
    if skill is not None and transcripts is not None:
        failing = edges_failing_threshold(skill, transcripts, threshold=threshold)
        if not failing:
            return []
        remove = [
            {
                "consumer_step": edge.consumer_step,
                "source_step": edge.source_step,
                "output": edge.output,
                "input": edge.binding.input,
            }
            for edge in failing
        ]
        fake_edge_failures = fake_edge_failure_count(skill, transcripts)
    elif fake_edge_failures is None:
        return []
    elif fake_edge_failures < threshold:
        return []

    assert fake_edge_failures is not None
    if fake_edge_failures < threshold and not remove:
        return []
    return [
        Proposal(
            kind="parallelise",
            skill_id=skill_id,
            version=version + 1,
            rationale=f"remove fake edges after {fake_edge_failures} failures",
            payload={
                "remove_input_binding": True,
                "bindings": remove,
                "fake_edge_failures": fake_edge_failures,
                "expected_parallel_speedup_note": "report beside merge_gap_rate only",
            },
        )
    ]

def propose_serialise(
    skill_id: str,
    version: int,
    *,
    merge_conflict_count: int | None = None,
    merge_audits: list[dict] | None = None,
    threshold: int = 5,
) -> list[Proposal]:
    """Propose adding serial edges when parallel waves lose work (merge conflicts/gaps)."""

    conflicts = merge_conflict_count
    if conflicts is None and merge_audits is not None:
        conflicts = sum(
            1
            for audit in merge_audits
            if audit.get("missing") or audit.get("conflict") or audit.get("incomplete")
        )
    if conflicts is None or conflicts < threshold:
        return []
    return [
        Proposal(
            kind="serialise",
            skill_id=skill_id,
            version=version + 1,
            rationale=f"add serial binding after {conflicts} merge conflicts/gaps",
            payload={
                "add_input_binding": True,
                "merge_conflict_count": conflicts,
                "expected_parallel_speedup_note": "speedup alone is refused without merge_gap_rate",
            },
        )
    ]

def correction_miner_from_reviewer_edits(
    edits: list[dict],
    *,
    threshold: int = 2,
) -> list[Proposal]:
    """Cluster reviewer edits into T2 ``correction`` proposals (never self-apply)."""

    clusters: dict[str, list[dict]] = {}
    for edit in edits:
        skill_id = str(edit.get("skill_id") or "").strip()
        if not skill_id:
            continue
        clusters.setdefault(skill_id, []).append(edit)

    proposals: list[Proposal] = []
    for skill_id, group in clusters.items():
        if len(group) < threshold:
            continue
        version = int(group[-1].get("version") or 1)
        proposals.append(
            Proposal(
                kind="correction",
                skill_id=skill_id,
                version=version,
                rationale=f"reviewer-edit cluster size={len(group)} (T2; human gate)",
                payload={
                    "tier": "T2",
                    "edit_count": len(group),
                    "edits": group,
                    "self_apply": False,
                },
            )
        )
    return proposals

def load_reviewer_edits(path: Path | str | None) -> list[dict]:
    if path is None:
        return []
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out
