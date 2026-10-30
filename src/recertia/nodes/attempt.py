"""Attempt-scoped spend accounting for ``solve`` (specs §10.1, §18).

``RunState.spent`` has exactly one writer: :class:`AttemptMeter`. Every solve path opens a
meter, charges what it uses, and commits once — so no path can execute work without charging
for it, and no path can forget a dimension.

The reason a meter is needed rather than direct reads of the runtime is lifetime mismatch.
``ModelClient.spend``, ``ToolRuntime.invocations`` and ``ClaimScheduler.conflicts`` are
*cumulative over the whole run*, while spend is charged *per attempt*. Reading a cumulative
counter as if it were an attempt delta charges every attempt for all preceding attempts too,
which compounds: four attempts of two tool calls each bill twenty. :class:`RuntimeWindow`
turns those counters into deltas, and the delta is what gets charged and persisted.

Charging is explicit rather than inferred from the runtime because ``ctx.op_once`` replays a
memoised result on resume without re-invoking the tool. An op that dispatched a tool call in
the original run must still be charged when its result is replayed, otherwise a resumed run
under-reports what it actually spent.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from contracts.budget import Budget, BudgetReservation, Spend, budget_excess
from contracts.failure import FailureSignal
from contracts.run import Artifact, RunState
from recertia.nodes.context import NodeContext, NodeOutcome

if TYPE_CHECKING:
    from contracts.resources import ResourceConflict
    from recertia.solver.registry import ToolResult

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class UsageDelta:
    """Resources a bounded window of work added to the run-scoped runtime counters."""

    tool_calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0

    def as_dict(self) -> dict[str, float | int]:
        """JSON-safe form, so a delta survives inside a persisted ``op_once`` result."""

        return {
            "tool_calls": self.tool_calls,
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "UsageDelta":
        return cls(
            tool_calls=int(payload.get("tool_calls", 0)),
            tokens=int(payload.get("tokens", 0)),
            cost_usd=float(payload.get("cost_usd", 0.0)),
        )


class RuntimeWindow:
    """What the model client and tool runtime recorded since this window opened.

    A window is the only sanctioned way to read the run-scoped counters, because it reads
    them twice and reports the difference.
    """

    def __init__(self, ctx: NodeContext) -> None:
        self._model = ctx.model
        self._tools = ctx.tools
        self._tokens_at_open = self._model.spend.tokens if self._model is not None else 0
        self._cost_at_open = self._model.spend.cost_usd if self._model is not None else 0.0
        self._invocations_at_open = len(self._tools.invocations) if self._tools is not None else 0
        self._conflicts_at_open = (
            len(self._tools.scheduler.conflicts) if self._tools is not None else 0
        )

    def delta(self) -> UsageDelta:
        tokens = 0
        cost = 0.0
        if self._model is not None:
            tokens = self._model.spend.tokens - self._tokens_at_open
            cost = self._model.spend.cost_usd - self._cost_at_open
        return UsageDelta(
            tool_calls=len(self.new_invocations()),
            tokens=max(0, tokens),
            cost_usd=max(0.0, cost),
        )

    def new_invocations(self) -> tuple["ToolResult", ...]:
        if self._tools is None:
            return ()
        return tuple(self._tools.invocations[self._invocations_at_open :])

    def new_conflicts(self) -> tuple["ResourceConflict", ...]:
        if self._tools is None:
            return ()
        return tuple(self._tools.scheduler.conflicts[self._conflicts_at_open :])


@dataclass
class AttemptMeter:
    """The single writer of ``RunState.spent`` for one solve attempt.

    ``charge`` records resources the attempt has already consumed; ``preflight`` asks whether
    the next unit of work still fits, counting committed spend, outstanding reservations, and
    everything charged so far in this attempt; ``commit`` folds the attempt into run spend.
    """

    budget: Budget
    committed: Spend
    reserved: BudgetReservation = field(default_factory=BudgetReservation)
    clock: Callable[[], float] = time.monotonic
    _tool_calls: int = field(default=0, init=False)
    _tokens: int = field(default=0, init=False)
    _cost_usd: float = field(default=0.0, init=False)
    _started_at: float = field(init=False)

    def __post_init__(self) -> None:
        self._started_at = self.clock()

    @classmethod
    def open(
        cls,
        state: RunState,
        *,
        reserved: BudgetReservation | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> "AttemptMeter":
        """Start metering against a run's committed spend and outstanding reservations.

        ``reserved`` is overridable because a path that is retiring its own reservation into
        spend must not also be charged for still holding it.
        """

        return cls(
            budget=state.budget,
            committed=state.spent,
            reserved=reserved if reserved is not None else state.reserved,
            clock=clock,
        )

    def charge(self, *, tool_calls: int = 0, tokens: int = 0, cost_usd: float = 0.0) -> None:
        self._tool_calls += tool_calls
        self._tokens += tokens
        self._cost_usd += cost_usd

    def charge_delta(self, delta: UsageDelta) -> None:
        self.charge(
            tool_calls=delta.tool_calls, tokens=delta.tokens, cost_usd=delta.cost_usd
        )

    @property
    def elapsed_s(self) -> float:
        return max(0.0, self.clock() - self._started_at)

    def used(
        self,
        *,
        attempts: int = 0,
        tool_calls: int = 0,
        tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> BudgetReservation:
        """Everything this attempt has consumed, optionally plus work about to be requested."""

        return BudgetReservation(
            attempts=attempts,
            tool_calls=self._tool_calls + tool_calls,
            tokens=self._tokens + tokens,
            cost_usd=self._cost_usd + cost_usd,
            wall_clock_s=self.elapsed_s,
        )

    def preflight(
        self,
        *,
        attempts: int = 0,
        tool_calls: int = 0,
        tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> str | None:
        """Return the first exhausted budget dimension, or ``None`` when the work fits."""

        return budget_excess(
            self.budget,
            self.committed,
            self.reserved,
            self.used(
                attempts=attempts, tool_calls=tool_calls, tokens=tokens, cost_usd=cost_usd
            ),
        )

    def commit(self, *, attempts: int = 1) -> Spend:
        """Fold this attempt's usage — including its wall clock — into run spend."""

        return self.committed.model_copy(
            update={
                "attempts": self.committed.attempts + attempts,
                "tool_calls": self.committed.tool_calls + self._tool_calls,
                "tokens": self.committed.tokens + self._tokens,
                "wall_clock_s": self.committed.wall_clock_s + self.elapsed_s,
                "cost_usd": self.committed.cost_usd + self._cost_usd,
            }
        )


def charge_version_write(state: RunState) -> RunState:
    """Sole writer of ``spent.versions_written`` (ADR-0017). Store hop only.

    Attempt-scoped dimensions stay on :class:`AttemptMeter`. Version writes happen at
    ``store``, not ``solve``, so they are not folded into ``commit``.
    """

    return state.model_copy(
        update={
            "spent": state.spent.model_copy(
                update={"versions_written": state.spent.versions_written + 1}
            )
        }
    )


def record_new_affordances(ctx: NodeContext, window: RuntimeWindow) -> None:
    """Record only what this window observed; the affordance store outlives the attempt."""

    if ctx.affordances is None:
        return
    invocations = window.new_invocations()
    conflicts = window.new_conflicts()
    if not invocations and not conflicts:
        return
    try:
        for result in invocations:
            ctx.affordances.record_tool(result)
        for conflict in conflicts:
            ctx.affordances.record_conflict(conflict)
        ctx.affordances.save()
    except sqlite3.Error as exc:
        # Affordances are advisory history; a locked or failing runtime.db must not fail
        # the attempt that produced them.
        _log.warning("affordance record skipped: %s: %s", type(exc).__name__, exc)


def failed(
    state: RunState,
    meter: AttemptMeter,
    *,
    signal: FailureSignal,
    attempt_no: int,
    route: str = "pre_validation_failure_signal",
    note: str | None = None,
    attempts: int = 1,
    updates: dict | None = None,
) -> NodeOutcome:
    """Close a failed attempt: charge it, record the signal, hand routing to the graph."""

    return NodeOutcome(
        state=state.model_copy(
            update={
                "attempt_no": attempt_no,
                "spent": meter.commit(attempts=attempts),
                "failure_signal": signal,
                **(updates or {}),
            }
        ),
        route=route,
        note=note,
    )


def completed(
    state: RunState,
    meter: AttemptMeter,
    *,
    attempt_no: int,
    transcript_ref: str,
    description: str | None = None,
    note: str | None = None,
    attempts: int = 1,
    updates: dict | None = None,
) -> NodeOutcome:
    """Close a completed attempt: charge it and leave judging to ``validate``.

    ``description`` attaches the transcript as an artifact; paths whose transcript is a
    per-branch pointer rather than a single reviewable artifact omit it.
    """

    artifacts = (
        {"artifacts": [*state.artifacts, Artifact(kind="text", ref=transcript_ref, description=description)]}
        if description is not None
        else {}
    )
    return NodeOutcome(
        state=state.model_copy(
            update={
                "attempt_no": attempt_no,
                "spent": meter.commit(attempts=attempts),
                "transcript_ref": transcript_ref,
                **artifacts,
                "failure_signal": None,
                **(updates or {}),
            }
        ),
        route="attempt_completed",
        note=note,
    )
