"""Systems projections: stuck jobs, lift by class, redundancy (ADR-0019)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from recertia.ops.systems import snapshot_from_events


@dataclass
class LiftCell:
    task_class: str
    established: bool
    detail: str
    lift: float | None = None


@dataclass
class OperatorBrief:
    stuck_jobs: list[dict[str, Any]] = field(default_factory=list)
    lift_by_task_class: list[LiftCell] = field(default_factory=list)
    redundancy: dict[str, Any] = field(default_factory=dict)
    unavailable: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stuck_jobs": self.stuck_jobs,
            "lift_by_task_class": [
                {
                    "task_class": c.task_class,
                    "established": c.established,
                    "detail": c.detail,
                    "lift": c.lift,
                }
                for c in self.lift_by_task_class
            ],
            "redundancy": self.redundancy,
            "unavailable": self.unavailable,
        }


def brief_from_events(
    events: list[Any],
    *,
    task_classes: list[str] | None = None,
    stuck: list[dict[str, Any]] | None = None,
) -> OperatorBrief:
    snap = snapshot_from_events(events)
    cells: list[LiftCell] = []
    for name in task_classes or []:
        cells.append(
            LiftCell(
                task_class=name,
                established=False,
                detail="not established: sample size below min_independent_runs",
            )
        )
    return OperatorBrief(
        stuck_jobs=list(stuck or []),
        lift_by_task_class=cells,
        redundancy={
            "tool_redundancy_rate": snap.tool_redundancy_rate,
            "retrieve_redundancy_rate": snap.retrieve_redundancy_rate,
        },
        unavailable=[] if events else ["no telemetry events"],
    )


def brief_from_runs_root(runs_root: Path | str) -> OperatorBrief:
    """Best-effort folder scan. Honest empty when stores are missing."""

    from contracts.policy import COMPUTER_USE_TASK_CLASSES
    from recertia.graph.store import CheckpointStore

    root = Path(runs_root)
    db = root / "checkpoints.db"
    unavailable: list[str] = []
    stuck: list[dict[str, Any]] = []
    if not db.exists():
        unavailable.append("checkpoints missing")
    else:
        store = CheckpointStore(db)
        try:
            for run_id in store.list_run_ids():
                latest = store.latest(run_id)
                if latest is None:
                    continue
                seq, node, _next, state = latest
                if state.terminal is None:
                    stuck.append({"run_id": run_id, "node": node, "seq": seq})
        finally:
            store.close()
    brief = brief_from_events([], task_classes=list(COMPUTER_USE_TASK_CLASSES), stuck=stuck)
    brief.unavailable.extend(unavailable)
    return brief
