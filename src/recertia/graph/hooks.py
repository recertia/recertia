"""Per-hop callbacks. The engine loop does not name intake, validate, or telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from contracts.run import RunState
from contracts.trajectory import TrajectoryEvent
from recertia.nodes.context import NodeOutcome


@dataclass(frozen=True)
class Hop:
    run_id: str
    tenant: str
    node: str
    attempt_no: int
    seq: int
    workdir: Path


class HopHook(Protocol):
    def before(self, hop: Hop) -> None: ...

    def after(
        self,
        hop: Hop,
        outcome: NodeOutcome,
        *,
        hop_ms: float,
        idle_gap_ms: float,
    ) -> None: ...


class RunCancelled(RuntimeError):
    """The queue asked this run to stop at a hop boundary."""


class CancelHopHook:
    """Raise ``RunCancelled`` when the caller-supplied flag is set."""

    def __init__(self, cancelled: Any) -> None:
        self._cancelled = cancelled

    def before(self, hop: Hop) -> None:
        if self._cancelled():
            raise RunCancelled(hop.run_id)

    def after(
        self,
        hop: Hop,
        outcome: NodeOutcome,
        *,
        hop_ms: float,
        idle_gap_ms: float,
    ) -> None:
        del hop, outcome, hop_ms, idle_gap_ms


class StateHop(Protocol):
    """A post-node adjustment that may replace run state. Failures propagate."""

    def adjust(
        self,
        orch: Any,
        *,
        node: str,
        state: RunState,
        attempt_no: int,
    ) -> tuple[RunState, list[TrajectoryEvent]]: ...


class TelemetryHopHook:
    """Emits ``node.started`` and ``node.finished`` with the historical gauges."""

    def before(self, hop: Hop) -> None:
        from recertia.ops.systems import component_class
        from recertia.telemetry import emit_in_run

        emit_in_run(
            "node.started",
            node=hop.node,
            component_class=component_class(hop.node),
        )

    def after(
        self,
        hop: Hop,
        outcome: NodeOutcome,
        *,
        hop_ms: float,
        idle_gap_ms: float,
    ) -> None:
        from recertia.ops.systems import hop_finished_attrs
        from recertia.telemetry import emit_in_run

        emit_in_run(
            "node.finished",
            **hop_finished_attrs(
                hop.node,
                hop_ms=hop_ms,
                workdir=hop.workdir,
                idle_gap_ms=idle_gap_ms,
                tokens=int(getattr(outcome.state.spent, "tokens", 0) or 0),
            ),
        )


class IntakeBindHook:
    def adjust(
        self,
        orch: Any,
        *,
        node: str,
        state: RunState,
        attempt_no: int,
    ) -> tuple[RunState, list[TrajectoryEvent]]:
        del attempt_no
        if node != "intake":
            return state, []
        from recertia.mea.runtime import bind_after_intake

        updated = bind_after_intake(
            state,
            policy=orch.policy,
            store=orch.audited_states,
            ledger=orch.ledger,
        )
        return updated, []


class ValidateAuditHook:
    def adjust(
        self,
        orch: Any,
        *,
        node: str,
        state: RunState,
        attempt_no: int,
    ) -> tuple[RunState, list[TrajectoryEvent]]:
        if node != "validate":
            return state, []
        from recertia.mea.runtime import audit_after_validate

        updated, delta = audit_after_validate(
            state,
            store=orch.audited_states,
            attempt_no=attempt_no,
        )
        if delta is None:
            return updated, []
        event = orch._trajectory_emitter.from_auditor_delta(
            updated,
            node=node,
            attempt_no=attempt_no,
            delta=delta,
        )
        return updated, [event]


def default_hop_hooks() -> list[HopHook]:
    return [TelemetryHopHook()]


def default_state_hops() -> list[StateHop]:
    return [IntakeBindHook(), ValidateAuditHook()]
