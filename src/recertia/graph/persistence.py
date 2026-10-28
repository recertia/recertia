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
    import_lock_timeout_s,
    legacy_import_complete,
    legacy_sqlite_pending,
    sqlite_has_table,
)
from recertia.persistence.layout import RunsRootLayout, RuntimeLayout
from recertia.persistence.root_lock import RootBusy, RootLockBusy, TenantLock
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
    root_lock: TenantLock | None = None

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
    runtime_root: Path | str | None = None,
) -> RunPersistence:
    """Open checkpoints, operations, the ledger, trajectories, audits, and snapshots.

    ``RECERTIA_RUNTIME_DB_MIGRATE=auto`` (default) imports leftover
    ``checkpoints.db`` / ``operations.db`` into ``runtime.db``. ``off`` keeps the
    leftover files (a copy, not a rename) for one-release rollback. After an
    import has been recorded in ``schema_migrations``, ``off`` reads
    ``runtime.db`` so post-import writes cannot vanish. ``manual`` refuses to
    start while leftover files are still pending. Anyone who finds a lock it
    needs (the root ``.lock`` or its tenant lock) held exclusively waits, then
    raises ``RootBusy`` and must not write somewhere else. There is no
    same-thread holder exception.

    On an already-imported (or fresh) root the handle holds the runtime root's
    ``.lock`` shared and then its runs root's tenant lock shared, for its
    lifetime (released by ``close``; REC-065 lock order, see
    :class:`~recertia.persistence.root_lock.TenantLock`). So it cannot write while
    backup or restore holds the root lock exclusively, nor while an import of
    the same tenant holds its tenant lock exclusively; handles and imports of
    other tenants on the same root do not wait for it. Taking both waits at
    most ``RECERTIA_IMPORT_LOCK_TIMEOUT_S`` in total, then raises ``RootBusy``
    before any db file is opened or written. Leftover files are re-checked once the lock
    is held: if a restore brought some back while this open waited, it raises
    ``RootBusy`` too, and the retry imports them. ``migrate=off`` re-checks too:
    if it chose the leftover files but the root was imported while it waited (a
    restore of a post-import backup), it raises ``RootBusy`` rather than write to
    files no later open reads.

    A bare path is its own runtime root (the CLI layout), so its ``.lock`` is
    ``<path>/.lock`` and its tenant lock ``<path>/.tenant.lock``. Pass
    ``runtime_root`` when ``root`` is a tenant runs directory under a runtime
    root (workers use ``<runtime_root>/runs/<tenant>``): the handle then locks
    ``<runtime_root>/.lock``, the lock backup and restore take, and then
    ``<root>/.tenant.lock``. Files and tenant id are unchanged.
    """

    del durability
    if isinstance(root, RunsRootLayout):
        if runtime_root is not None:
            raise ValueError("runtime_root applies to a path root, not a RunsRootLayout")
        layout = root
    else:
        path = Path(root)
        path.mkdir(parents=True, exist_ok=True)
        if runtime_root is None:
            layout = RuntimeLayout(path).local_runs()
        else:
            layout = RunsRootLayout(parent=RuntimeLayout(Path(runtime_root)), tenant=None, runs_root=path)
    tenant = str(layout.tenant) if layout.tenant is not None else "local"
    mode = migrate if migrate is not None else load_settings().runtime_db_migrate
    use_runtime, _ = _resolve_runtime_db(layout, mode)
    # Root lock shared, then this runs root's tenant lock shared (REC-065 lock order).
    shared = TenantLock(layout.parent.root_lock, layout.tenant_lock)
    if use_runtime:
        try:
            shared.acquire(exclusive=False, timeout_s=import_lock_timeout_s())
        except RootLockBusy as exc:
            raise RootBusy(f"cannot open the runtime root while the {shared.refused}") from exc
        if legacy_sqlite_pending(layout):
            # Decided before the lock; a restore may have brought leftover files back.
            shared.release()
            raise RootBusy(
                f"leftover runtime files appeared while waiting for the root lock: {shared.root.path}"
            )
    else:
        # REC-064: the leftover (migrate=off) path is also bounded. It used to
        # block forever; now it waits at most RECERTIA_IMPORT_LOCK_TIMEOUT_S
        # and raises typed RootBusy, writing nothing.
        try:
            shared.acquire(exclusive=False, timeout_s=import_lock_timeout_s())
        except RootLockBusy as exc:
            raise RootBusy(f"cannot open the runtime root while the {shared.refused}") from exc
        if _resolve_runtime_db(layout, "off")[0]:
            # migrate=off decided "leftover files" before the lock. A restore that ran
            # meanwhile may have brought back an imported runtime.db, which every later
            # open treats as the source of truth; a write to the leftover files would
            # be lost (REC-058). Fail closed; the retry reads runtime.db.
            shared.release()
            raise RootBusy(
                f"the runtime root was imported while waiting for the root lock: {shared.root.path}"
            )
    handle_lock: TenantLock | None = shared
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
            ),
            operations=OperationLedger(
                operations_db,
                lease_db=layout.runtime_db,
                tenant_id=tenant,
                schema_lock=use_runtime,
            ),
            ledger=HashChainLedger(
                layout.ledger_path,
                lease_db=layout.runtime_db,
                tenant_id=tenant,
            ),
            trajectories=TrajectoryStore(layout.trajectories_dir),
            audited_states=AuditedStateStore(layout.audited_states_dir),
            workspaces=WorkspaceManager(layout.snapshots_dir),
            root_lock=handle_lock,
        )
        handle_lock = None
        return persist
    finally:
        if handle_lock is not None:
            handle_lock.release()


# The condition comes first and matters: an older release's `migrate=off` handle keeps only
# the root lock shared, so a newer import does not see it and its later writes would be
# lost. Otherwise the caller stays on `off`. A test pins the order, the condition, the
# retry hint and the settings named here.
MANUAL_MODE_PENDING_ACTION = (
    "Only once no process of an older release uses this root, set"
    " RECERTIA_RUNTIME_DB_MIGRATE=auto (the default) and reopen; the open that gets the"
    " locks imports the pending files into runtime.db, so retry on root_busy. Otherwise set"
    " RECERTIA_RUNTIME_DB_MIGRATE=off and stay on it until no older-release process is left"
)


def _resolve_runtime_db(layout: RunsRootLayout, mode: MigrateMode) -> tuple[bool, Path | None]:
    if mode == "off":
        # After import, runtime.db is the source of truth even though leftover
        # files are kept for rollback (probe K / Q).
        if legacy_import_complete(layout.runtime_db):
            return True, None
        if layout.checkpoints_db.is_file() or layout.operations_db.is_file():
            return False, None
        if sqlite_has_table(layout.runtime_db, "checkpoints") or sqlite_has_table(
            layout.runtime_db, "operations"
        ):
            return True, None
        return False, None
    if mode == "manual":
        if legacy_sqlite_pending(layout):
            # No CLI subcommand runs this import yet (#172); the `auto` open is the
            # import. A test checks that every `recertia ...` command named here exists.
            raise MigrationChecksumError(
                "pending legacy runtime files; only once no process of an older release"
                " uses this root, reopen with RECERTIA_RUNTIME_DB_MIGRATE=auto (the default)"
                " to import them; otherwise stay on RECERTIA_RUNTIME_DB_MIGRATE=off",
                details={"action": MANUAL_MODE_PENDING_ACTION},
            )
        return True, None
    imported = import_legacy_runtime(layout)
    if imported:
        return True, None
    ckpt_fb = layout.checkpoints_db if layout.checkpoints_db.is_file() else None
    return True, ckpt_fb
