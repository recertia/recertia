"""The six run stores, opened once and shared with the applicator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from contracts.run import RunState
from recertia.graph.lease import require_lease_token
from recertia.graph.ops import OperationLedger, ensure_operations_schema
from recertia.graph.store import CheckpointStore, EvalOutboxWrite
from recertia.ledger import HashChainLedger
from recertia.mea.store import AuditedStateStore
from recertia.persistence.db import MigrationChecksumError, apply_migrations, open_db
from recertia.persistence.import_legacy import (
    import_legacy_runtime,
    legacy_import_complete,
    legacy_sqlite_pending,
    sqlite_has_table,
)
from recertia.persistence.layout import RunsRootLayout, RuntimeLayout
from recertia.persistence.root_lock import RootLock
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
    root_lock: RootLock | None = None

    def close(self) -> None:
        try:
            self.checkpoints.close()
        finally:
            try:
                self.operations.close()
            finally:
                lock = self.root_lock
                self.root_lock = None
                if lock is not None:
                    lock.release()



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
    leftover files (a copy, not a rename) for one-release rollback. After an
    import has been recorded in ``schema_migrations``, ``off`` reads
    ``runtime.db`` so post-import writes cannot vanish. ``manual`` refuses to
    start while leftover files are still pending. A busy root lock overlays
    leftover reads onto ``runtime.db`` writes so unimported rows stay visible.
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
    use_runtime, read_fallback, ops_fallback = _resolve_runtime_db(layout, mode)
    leftover_lock: RootLock | None = None
    if not use_runtime:
        leftover_lock = RootLock(layout.parent.root_lock)
        leftover_lock.acquire_shared()
    try:
        if use_runtime:
            conn = open_db(layout.runtime_db)
            try:
                apply_migrations(conn, _RUNTIME_MIGRATIONS)
                # Baseline operations PK has no incarnation. Rebuild here so
                # OperationLedger.__init__ does not block on a busy root lock.
                ensure_operations_schema(conn)
            finally:
                conn.close()
            checkpoint_db = layout.runtime_db
            operations_db = layout.runtime_db
        else:
            checkpoint_db = layout.checkpoints_db
            operations_db = layout.operations_db
        persist = RunPersistence(
            checkpoints=CheckpointStore(
                checkpoint_db,
                lease_db=layout.runtime_db,
                tenant_id=tenant,
                read_fallback=read_fallback,
            ),
            operations=OperationLedger(
                operations_db,
                lease_db=layout.runtime_db,
                tenant_id=tenant,
                schema_lock=use_runtime,
                read_fallback=ops_fallback,
            ),
            ledger=HashChainLedger(
                layout.ledger_path,
                lease_db=layout.runtime_db,
                tenant_id=tenant,
            ),
            trajectories=TrajectoryStore(layout.trajectories_dir),
            audited_states=AuditedStateStore(layout.audited_states_dir),
            workspaces=WorkspaceManager(layout.snapshots_dir),
            root_lock=leftover_lock,
        )
        leftover_lock = None
        return persist
    finally:
        if leftover_lock is not None:
            leftover_lock.release()


def _resolve_runtime_db(
    layout: RunsRootLayout, mode: MigrateMode
) -> tuple[bool, Path | None, Path | None]:
    if mode == "off":
        # After import, runtime.db is the source of truth even though leftover
        # files are kept for rollback (probe K / Q).
        if legacy_import_complete(layout.runtime_db):
            return True, None, None
        if layout.checkpoints_db.is_file() or layout.operations_db.is_file():
            return False, None, None
        if sqlite_has_table(layout.runtime_db, "checkpoints") or sqlite_has_table(
            layout.runtime_db, "operations"
        ):
            return True, None, None
        return False, None, None
    if mode == "manual":
        if legacy_sqlite_pending(layout):
            raise MigrationChecksumError(
                "pending legacy runtime files; run recertia migrate apply",
                details={"action": "recertia migrate apply"},
            )
        return True, None, None
    imported = import_legacy_runtime(layout)
    ckpt_fb = layout.checkpoints_db if not imported and layout.checkpoints_db.is_file() else None
    ops_fb = layout.operations_db if not imported and layout.operations_db.is_file() else None
    return True, ckpt_fb, ops_fb
