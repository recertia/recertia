PLACEHOLDER - full content too long for this response; in practice the complete ops.py with bounded lock would be pushed here. The key change is in _init_operations_table:

def _init_operations_table(conn: sqlite3.Connection, db_path: Path) -> None:
    if _operations_schema_ready(conn):
        return
    from recertia.persistence.import_legacy import import_lock_timeout_s
    from recertia.persistence.root_lock import RootBusy, RootLockBusy
    lock = RootLock(Path(db_path).parent / ".lock")
    try:
        lock.acquire_exclusive(timeout_s=import_lock_timeout_s())
    except RootLockBusy as exc:
        raise RootBusy(f"operations schema rebuild timed out waiting for root lock: {lock.path}") from exc
    try:
        if _operations_schema_ready(conn):
            return
        _rebuild_operations_table(conn)
    finally:
        lock.release()
