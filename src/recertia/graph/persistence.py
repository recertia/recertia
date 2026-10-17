"""The six run stores, opened once and shared with the applicator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from recertia.graph.ops import OperationLedger
from recertia.graph.store import CheckpointStore
from recertia.ledger import HashChainLedger
from recertia.mea.store import AuditedStateStore
from recertia.persistence.layout import RunsRootLayout, RuntimeLayout
from recertia.trajectory.store import TrajectoryStore
from recertia.workspace import WorkspaceManager


@dataclass
class RunPersistence:
    checkpoints: CheckpointStore
    operations: OperationLedger
    ledger: HashChainLedger
    trajectories: TrajectoryStore
    audited_states: AuditedStateStore
    workspaces: WorkspaceManager

    def close(self) -> None:
        self.checkpoints.close()
        self.operations.close()


def open_run_persistence(
    root: Path | str | RunsRootLayout,
    *,
    durability: str = "normal",
) -> RunPersistence:
    """Open checkpoints, operations, the ledger, trajectories, audits, and snapshots.

    ``durability`` is accepted for the REL-3 store swap. The current constructors
    still take their synchronous mode from Settings.
    """

    del durability
    if isinstance(root, RunsRootLayout):
        layout = root
    else:
        path = Path(root)
        path.mkdir(parents=True, exist_ok=True)
        layout = RuntimeLayout(path).local_runs()
    return RunPersistence(
        checkpoints=CheckpointStore(layout.checkpoints_db),
        operations=OperationLedger(layout.operations_db),
        ledger=HashChainLedger(layout.ledger_path),
        trajectories=TrajectoryStore(layout.trajectories_dir),
        audited_states=AuditedStateStore(layout.audited_states_dir),
        workspaces=WorkspaceManager(layout.snapshots_dir),
    )
