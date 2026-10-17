"""Active-set and retirement proposals."""

from __future__ import annotations

from pathlib import Path

from contracts.replay import WorldState
from recertia.evals.store import EvalStore
from recertia.jobs import Proposal
from recertia.memory.procedural.store import SkillStore
from recertia.review.autonomy_config import DEFAULT_AUTONOMY, AutonomyConfig
from recertia.trajectory.store import TrajectoryStore


def curator_active_set_and_dedup(
    store: SkillStore,
    *,
    trajectory_store: TrajectoryStore | None = None,
    replay_limit: int = 50,
    eval_store: EvalStore | None = None,
    config: AutonomyConfig | None = None,
    ledger=None,
    existing_proposals: list[Proposal] | None = None,
    proposals_path: Path | None = None,
) -> list[Proposal]:
    """Recompute the active set, propose retirements, attach retrieval-only ReplayPacks."""

    from recertia.memory.procedural.active_set import recompute_active_set
    from recertia.memory.procedural.portfolio import propose_retirements, rank_skills
    from recertia.replay.pack import build_replay_pack
    from recertia.replay.sample import sample_trajectories_for_skill
    from recertia.review.lifecycle import LifecycleError, maybe_bench_on_contribution

    cfg = config or DEFAULT_AUTONOMY
    _updated, pressure = recompute_active_set(store, config=cfg, eval_store=eval_store)
    mean_pressure = (
        sum(pressure.values()) / len(pressure) if pressure else 0.0
    )
    proposals: list[Proposal] = [
        Proposal(
            kind="curate",
            skill_id="active-set",
            version=0,
            rationale=f"recomputed active set; pressure={pressure}",
            payload={
                "pressure": pressure,
                "active_cap_pressure": mean_pressure,
            },
        )
    ]

    # Cap pass must not bench. Retirement is a separate evidence-of-harm decision.
    if eval_store is not None:
        by_class: dict[str, list] = {}
        for version, status, stats in store.iter_loaded():
            if status.lifecycle == "approved":
                by_class.setdefault(version.task_class, []).append((version, status, stats))
        for _task_class, rows in by_class.items():
            for proposal in propose_retirements(rank_skills(rows, cfg), cfg):
                try:
                    maybe_bench_on_contribution(
                        store,
                        proposal.skill_id,
                        proposal.version,
                        eval_store=eval_store,
                        config=cfg,
                        ledger=ledger,
                    )
                    proposals.append(
                        Proposal(
                            kind="curate",
                            skill_id=proposal.skill_id,
                            version=proposal.version,
                            rationale=(
                                f"benched {proposal.skill_id}@v{proposal.version}: "
                                f"{proposal.evidence}"
                            ),
                            payload={
                                "retirement": True,
                                "evidence": proposal.evidence,
                                "estimate": proposal.contribution_estimate,
                            },
                        )
                    )
                except LifecycleError:
                    continue

    flagged = 0
    from recertia.jobs.proposals_log import ProposalLog
    from recertia.memory.procedural.lint import lint_report

    existing = list(existing_proposals or [])
    if proposals_path is not None:
        existing.extend(ProposalLog(proposals_path).load())
    already = {
        (p.skill_id, p.version, tuple(sorted(set(p.payload.get("codes") or ()))))
        for p in existing
        if p.payload.get("specificity")
    }
    seen_specificity: set[tuple[str, int, tuple[str, ...]]] = set(already)
    new_specificity: list[Proposal] = []

    for version, status, stats in store.iter_loaded():
        if flagged >= 8:
            break
        if status.lifecycle != "approved" or not status.active:
            continue
        report = lint_report(
            version, status, stats, store=store, skip_if_hash_matches=False
        )
        hits = [finding for finding in report.findings if finding.code in {"SPEC", "VAGUE"}]
        if not hits:
            continue
        codes = tuple(sorted({finding.code for finding in hits}))
        key = (version.skill_id, version.version, codes)
        if key in seen_specificity:
            continue
        seen_specificity.add(key)
        flagged_proposal = Proposal(
            kind="curate",
            skill_id=version.skill_id,
            version=version.version,
            rationale=(
                f"specificity review {version.skill_id}@v{version.version}: "
                + "; ".join(finding.code for finding in hits[:4])
            ),
            payload={
                "specificity": True,
                "codes": list(codes),
                "messages": [finding.message for finding in hits],
                "finding_hash": "-".join(codes),
            },
        )
        proposals.append(flagged_proposal)
        new_specificity.append(flagged_proposal)
        flagged += 1

    if proposals_path is not None:
        ProposalLog(proposals_path).append(new_specificity)

    if trajectory_store is None:
        return proposals


    packs_attached = 0
    max_packs = 5
    for version, status, _stats in store.iter_loaded():
        if packs_attached >= max_packs:
            break
        if status.lifecycle != "approved" or not status.active:
            continue
        trajectories = sample_trajectories_for_skill(
            trajectory_store, skill_id=version.skill_id, limit=replay_limit
        )
        if not trajectories:
            continue
        world = WorldState(
            suppressed_skill_ids=[version.skill_id],
            skill_status_overrides={
                f"{version.skill_id}@{version.version}": "benched"
            },
            library_commit=None,
            index_snapshot_id=None,
        )
        pack = build_replay_pack(
            trajectory_store,
            trajectories=trajectories,
            world=world,
            mode="retrieval_only",
            purpose="curator_counterfactual",
        )
        proposals.append(
            Proposal(
                kind="curate",
                skill_id=version.skill_id,
                version=version.version,
                rationale=(
                    f"replay pack ({len(pack.observations)} traj) for "
                    f"{version.skill_id}@v{version.version}"
                ),
                payload={
                    "replay_pack": pack.model_dump(mode="json"),
                    "purpose": "curator_counterfactual",
                },
            )
        )
        packs_attached += 1
    return proposals
