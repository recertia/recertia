"""Recertification proposals."""

from __future__ import annotations

from recertia.evals.store import EvalStore
from recertia.jobs import Proposal
from recertia.memory.procedural.store import SkillStore
from recertia.review.autonomy_config import DEFAULT_AUTONOMY


def recertify_stale(store: SkillStore, *, tool_upgraded: str | None = None) -> list[Proposal]:
    proposals: list[Proposal] = []
    for version, status, _stats in store.iter_loaded():
        if tool_upgraded or status.certification.recert_status == "stale":
            store.write_status(
                status.model_copy(update={"lifecycle": "needs_recert", "active": False})
            )
            proposals.append(
                Proposal(
                    kind="recertify",
                    skill_id=version.skill_id,
                    version=version.version,
                    rationale=f"tool upgrade or stale cert → needs_recert ({tool_upgraded})",
                )
            )
    return proposals

def recertify_with_revokes(
    store: SkillStore,
    *,
    lineage_index=None,
    revoke_queue=None,
    max_writes: int = 50,
    tool_upgraded: str | None = None,
    eval_store: EvalStore | None = None,
) -> list[Proposal]:
    """One Recertifier pass: stale certs + field off-ramp + lineage revoke drain."""

    proposals = recertify_stale(store, tool_upgraded=tool_upgraded)
    if eval_store is not None:
        from recertia.review.field_failures import recertify_field_failures

        for off_ramp in recertify_field_failures(store, eval_store, config=DEFAULT_AUTONOMY):
            proposals.append(
                Proposal(
                    kind="recertify",
                    skill_id=off_ramp.skill_id,
                    version=off_ramp.version,
                    rationale=(
                        f"field off-ramp: {off_ramp.consecutive_failures} consecutive "
                        "treatment-arm failures"
                    ),
                    payload={
                        "consecutive_field_failures": off_ramp.consecutive_failures,
                        "reason": "field_failures",
                    },
                )
            )
    if lineage_index is None or revoke_queue is None:
        return proposals
    from recertia.memory.procedural.lineage import drain_revokes

    touched = drain_revokes(store, lineage_index, revoke_queue, max_writes=max_writes)
    for status in touched:
        proposals.append(
            Proposal(
                kind="recertify",
                skill_id=status.skill_id,
                version=status.version,
                rationale="lineage revoke drain → needs_recert",
            )
        )
    return proposals
