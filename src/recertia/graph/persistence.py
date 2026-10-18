"""The six run stores, opened once and shared with the applicator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from contracts.run import RunState
from recertia.graph.lease import require_lease_token
from recertia.graph.ops import OperationLedger
from recertia.graph.store import CheckpointStore, EvalOutboxWrite
from recertia.ledger import HashChainLedger
from recertia.mea.store import AuditedStateStore
from recertia.persistence.db import MigrationChecksumError, apply_migrations, open_db
from recertia.persistence.import_legacy import import_legacy_runtime, legacy_sqlite_pending
from recertia.persistence.layout import RunsRootLayout, RuntimeLayout
from recertia.settings import MigrateMode, load_settings
from recertia.trajectory.store import TrajectoryStore
from recertia.workspace import WorkspaceManager

_RUNTIME_MIGRATIONS = Path(__file__).resolve().parents[1] / "persistence" / "migrations" / "runtime"


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



class FencedRunPersistence:
    """Checkpoint writes compare the caller token to ``run_leases`` (REL-3)."""

    def __init__(
        self,
        inner: RunPersistence,
        *,
        runtime_db: Path,
        tenant_id: str = "local",
    ) -> None:
        self.inner = inner
        self.runtime_db = Path(runtime_db)
        self.tenant_id = tenant_id

    def save_checkpoint(
        self,
        run_id: str,
        seq: int,
        node: str,
        next_node: str | None,
        state: RunState,
        *,
        fencing_token: int,
        eval_outbox: EvalOutboxWrite | None = None,
    ) -> None:
        require_lease_token(
            self.runtime_db,
            tenant_id=self.tenant_id,
            run_id=run_id,
            fencing_token=fencing_token,
        )
        self.inner.checkpoints.save(
            run_id,
            seq,
            node,
            next_node,
            state,
            fencing_token=fencing_token,
            eval_outbox=eval_outbox,
        )

    def close(self) -> None:
        self.inner.close()


def open_run_persistence(
    root: Path | str | RunsRootLayout,
    *,
    durability: str = "normal",
    migrate: MigrateMode | None = None,
) -> RunPersistence:
    """Open checkpoints, operations, the ledger, trajectories, audits, and snapshots.

    ``RECERTIA_RUNTIME_DB_MIGRATE=auto`` (default) imports leftover
    ``checkpoints.db`` / ``operations.db`` into ``runtime.db``. ``off`` keeps the
    legacy files for one release. ``manual`` refuses to start while they remain.
    """

    del durability
    if isinstance(root, RunsRootLayout):
        layout = root
    else:
        path = Path(root)
        path.mkdir(parents=True, exist_ok=True)
        layout = RuntimeLayout(path).local_runs()
    tenant = str(layout.tenant) if layout.tenant is not None else "local"
    mode = migrate if migrate is not None else load_settings().runtime_db_migrate
    use_runtime = _resolve_runtime_db(layout, mode)
    if use_runtime:
        conn = open_db(layout.runtime_db)
        try:
            apply_migrations(conn, _RUNTIME_MIGRATIONS)
        finally:
            conn.close()
        checkpoint_db = layout.runtime_db
        operations_db = layout.runtime_db
    else:
        checkpoint_db = layout.checkpoints_db
        operations_db = layout.operations_db
    return RunPersistence(
        checkpoints=CheckpointStore(
            checkpoint_db,
            lease_db=layout.runtime_db,
            tenant_id=tenant,
        ),
        operations=OperationLedger(operations_db),
        ledger=HashChainLedger(layout.ledger_path),
        trajectories=TrajectoryStore(layout.trajectories_dir),
        audited_states=AuditedStateStore(layout.audited_states_dir),
        workspaces=WorkspaceManager(layout.snapshots_dir),
    )


def _resolve_runtime_db(layout: RunsRootLayout, mode: MigrateMode) -> bool:
    if mode == "off":
        return False
    if mode == "manual":
        if legacy_sqlite_pending(layout):
            raise MigrationChecksumError(
                "pending legacy runtime files; run recertia migrate apply",
                details={"action": "recertia migrate apply"},
            )
        return True
    return import_legacy_runtime(layout)
