"""Pure trajectory event construction (ADR-0011). Engine persists via TrajectoryStore."""

from __future__ import annotations

from datetime import datetime, timezone

from contracts.audited_task_state import AuditorDelta
from contracts.run import RunState
from contracts.trajectory import TrajectoryEvent


class TrajectoryEmitter:
    """Build decision-boundary events from node outcomes. Never writes storage."""

    def __init__(
        self,
        *,
        memory_snapshot_id: str | None = None,
        library_commit: str | None = None,
        model_ref: str | None = None,
        policy_version: str | None = None,
    ) -> None:
        self.memory_snapshot_id = memory_snapshot_id
        self.library_commit = library_commit
        self.model_ref = model_ref
        self.policy_version = policy_version

    def _base(
        self,
        state: RunState,
        *,
        node: str,
        attempt_no: int,
        event_kind: str,
        summary: str | None = None,
        payload_inline: dict | None = None,
        skill_id: str | None = None,
        skill_version: int | None = None,
        criterion_id: str | None = None,
        branch_id: str | None = None,
    ) -> TrajectoryEvent:
        return TrajectoryEvent(
            run_id=state.run_id,
            seq=0,  # store assigns
            node=node,
            attempt_no=attempt_no,
            event_kind=event_kind,  # type: ignore[arg-type]
            at=datetime.now(timezone.utc),
            memory_snapshot_id=self.memory_snapshot_id or state.manifest.index_snapshot_id,
            library_commit=self.library_commit or state.manifest.library_commit,
            criteria_hash=state.manifest.criteria_hash,
            model_ref=self.model_ref
            or (
                f"{state.manifest.model}:{state.manifest.model_version}"
                if state.manifest.model
                else None
            ),
            policy_version=self.policy_version or state.manifest.policy_version,
            summary=summary,
            payload_inline=payload_inline,
            skill_id=skill_id,
            skill_version=skill_version,
            criterion_id=criterion_id,
            branch_id=branch_id,
        )

    def from_node_outcome(
        self,
        *,
        prior: RunState,
        new_state: RunState,
        node: str,
        attempt_no: int,
        route: str | None,
        note: str | None,
    ) -> list[TrajectoryEvent]:
        """Emit the phase-1 required kinds based on state deltas."""

        events: list[TrajectoryEvent] = []
        if node == "intake" and new_state.manifest.criteria_hash:
            events.append(
                self._base(
                    new_state,
                    node=node,
                    attempt_no=attempt_no,
                    event_kind="criteria_locked",
                    summary=f"locked {len(new_state.criteria)} criteria",
                    payload_inline={"criteria_hash": new_state.manifest.criteria_hash},
                )
            )
        if node == "retrieve":
            skills = [
                {"skill_id": s.skill_id, "version": s.version, "score": s.score}
                for s in (new_state.bundle.skills if new_state.bundle else [])
            ]
            suppressed = new_state.arm == "control"
            events.append(
                self._base(
                    new_state,
                    node=node,
                    attempt_no=attempt_no,
                    event_kind="retrieval_result",
                    summary=f"bundle_size={len(skills)} suppressed={suppressed}",
                    payload_inline={"skills": skills, "suppressed": suppressed},
                )
            )
        if node == "plan" and new_state.strategy:
            chosen = new_state.chosen
            events.append(
                self._base(
                    new_state,
                    node=node,
                    attempt_no=attempt_no,
                    event_kind="plan_choice",
                    summary=f"strategy={new_state.strategy}",
                    payload_inline={
                        "strategy": new_state.strategy,
                        "strategy_reason": new_state.strategy_reason,
                        "route": route,
                    },
                    skill_id=chosen.skill_id if chosen else None,
                    skill_version=chosen.version if chosen else None,
                )
            )
        if node == "validate" and new_state.results:
            for result in new_state.results:
                events.append(
                    self._base(
                        new_state,
                        node=node,
                        attempt_no=attempt_no,
                        event_kind="criterion_scored",
                        summary=f"{result.criterion_id}={'pass' if result.passed else 'fail'}",
                        payload_inline={
                            "passed": result.passed,
                            "isolation": getattr(result, "isolation", None),
                            "context_hash": getattr(result, "context_hash", None),
                        },
                        criterion_id=result.criterion_id,
                    )
                )
        if node == "classify_failure" and new_state.failure is not None:
            events.append(
                self._base(
                    new_state,
                    node=node,
                    attempt_no=attempt_no,
                    event_kind="failure_classified",
                    summary=new_state.failure.failure_class,
                    payload_inline={
                        "failure_class": new_state.failure.failure_class,
                        "counts_against_trust": new_state.failure.counts_against_trust,
                    },
                )
            )
        if node == "evolve":
            events.append(
                self._base(
                    new_state,
                    node=node,
                    attempt_no=attempt_no,
                    event_kind="evolve_decision",
                    summary=note or route,
                    payload_inline={"strategy": new_state.strategy, "route": route},
                )
            )
        if node == "finalize" and new_state.terminal:
            events.append(
                self._base(
                    new_state,
                    node=node,
                    attempt_no=attempt_no,
                    event_kind="terminal",
                    summary=new_state.terminal,
                    payload_inline={
                        "terminal": new_state.terminal,
                        "attempt_no": new_state.attempt_no,
                        "cost_usd": new_state.spent.cost_usd,
                    },
                )
            )
        # Ignore unused prior in signature for API stability with engine.
        _ = prior
        return events

    def from_auditor_delta(
        self,
        state: RunState,
        *,
        node: str,
        attempt_no: int,
        delta: AuditorDelta,
    ) -> TrajectoryEvent:
        """Emit an accepted MEA CAS as ``audited_state_delta`` (ADR-0011)."""

        return self._base(
            state,
            node=node,
            attempt_no=attempt_no,
            event_kind="audited_state_delta",
            summary=(
                f"v{delta.parent_version}->{delta.proposed_version} "
                f"phase={delta.current_phase}"
            ),
            payload_inline=delta.model_dump(mode="json"),
        )
