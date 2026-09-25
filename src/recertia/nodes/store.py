"""``store``: transactional skill + facts write, index update, ledger append (M3)."""

from __future__ import annotations

from contracts.budget import BudgetReservation, budget_excess
from contracts.fact import Fact
from contracts.run import RunState
from contracts.skill import SkillVersion
from contracts.stats import SkillStats
from contracts.status import SkillStatus
from recertia.memory.procedural.hygiene import require_clean
from recertia.nodes._util import now
from recertia.nodes.attempt import charge_version_write
from recertia.nodes.context import NodeContext, NodeOutcome


def store(state: RunState, ctx: NodeContext) -> NodeOutcome:
    if not state.draft:
        raise ValueError("store called without a draft")
    if (
        budget_excess(
            state.budget,
            state.spent,
            state.reserved,
            BudgetReservation(versions_written=1),
        )
        == "versions_written"
    ):
        raise ValueError("store called with versions_written budget exhausted")

    def _write() -> dict:
        version = SkillVersion.model_validate(state.draft)
        version = require_clean(version)
        if ctx.store is None:
            raise ValueError("store node requires SkillStore on NodeContext")

        # Task-plane code is allowed to persist a reviewable candidate only.
        # `promote_to_approved` is the sole state transition to approved and
        # runs the golden regression gate before writing that lifecycle.
        ctx.store.write_candidate(version)

        written_facts: list[str] = []
        if ctx.facts is not None:
            for raw in state.facts_extracted:
                fact = Fact.model_validate(raw)
                stored = ctx.facts.write(fact)
                written_facts.append(stored.fact_id)

        if ctx.index is not None:
            # Index just the new candidate. write_candidate always persists exactly this
            # status/stats pair, and the refreshed fingerprint keeps startup rebuild-skip
            # accurate. Anything else that changed on disk (e.g. stats from applies) is
            # picked up by the next fingerprint-mismatch rebuild.
            ctx.index.upsert(
                version,
                SkillStatus(
                    skill_id=version.skill_id,
                    version=version.version,
                    lifecycle="candidate",
                    active=False,
                ),
                SkillStats(skill_id=version.skill_id, version=version.version),
                library_fingerprint=ctx.store.library_fingerprint(),
            )

        entry = ctx.ledger.append(
            actor=ctx.run_id,
            action="write",
            target=f"{version.skill_id}@v{version.version}",
            evidence={
                "run_id": ctx.run_id,
                "reusability": "reusable",
                "curation": version.provenance.curation,
                "authoring_prior_version": version.provenance.authoring_prior_version,
                "facts": written_facts,
            },
            at=now(),
        )
        return {
            "skill_id": version.skill_id,
            "version": version.version,
            "ledger_entry_seq": entry.seq,
            "facts": written_facts,
        }

    summary = ctx.op_once(0, _write)
    new_state = charge_version_write(
        state.model_copy(
            update={
                "written_versions": [
                    *state.written_versions,
                    {
                        "skill_id": summary["skill_id"],
                        "version": summary["version"],
                        "ledger_entry_seq": summary["ledger_entry_seq"],
                        "facts": summary["facts"],
                    },
                ]
            }
        )
    )
    return NodeOutcome(
        state=new_state,
        route="always",
        note=f"wrote {summary['skill_id']}@v{summary['version']} ledger={summary['ledger_entry_seq']}",
    )
