"""A failed ``open_run_persistence`` closes the stores it already built, then releases the lock.

Each case makes one later step raise. The checkpoint store and the operation ledger
built before it must refuse use afterwards (``sqlite3.ProgrammingError``: closed), the
caller must get the original exception, and the root and tenant locks must be free.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import recertia.graph.persistence as gp
from recertia.graph.ops import OperationLedger
from recertia.graph.store import CheckpointStore
from recertia.persistence.layout import RuntimeLayout
from recertia.persistence.root_lock import RootLock, RootLockBusy


class _InjectedOpenError(RuntimeError):
    """The fault each case injects into a later step of the open."""


class _InjectedCloseError(RuntimeError):
    """The fault case 7 injects into a cleanup ``close()``."""


def _locks_free(root: Path) -> bool:
    """True when the root lock and the tenant lock can both be taken exclusively now."""

    layout = RuntimeLayout(root).local_runs()
    for path in (layout.parent.root_lock, layout.tenant_lock):
        lock = RootLock(path)
        try:
            lock.try_exclusive()
        except RootLockBusy:
            return False
        lock.release()
    return True


def _boom(*_args: Any, **_kwargs: Any) -> Any:
    raise _InjectedOpenError("injected open failure")


def _record_real(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    real: Callable[..., Any],
    built: list[Any],
) -> None:
    """Replace ``gp.<name>`` with a wrapper that builds the real object and records it."""

    def build(*args: Any, **kwargs: Any) -> Any:
        obj = real(*args, **kwargs)
        built.append(obj)
        return obj

    monkeypatch.setattr(gp, name, build)


def _assert_checkpoints_closed(store: CheckpointStore) -> None:
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        store.latest_seq("run-1")


def _assert_operations_closed(ledger: OperationLedger) -> None:
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        ledger.get("run-1", 1, "node", 1)


# (constructor that raises, whether the operation ledger was built before it)
_FAULT_CASES = [
    pytest.param("OperationLedger", False, id="1-operation-ledger"),
    pytest.param("HashChainLedger", True, id="2-hash-chain-ledger"),
    pytest.param("TrajectoryStore", True, id="3-trajectory-store"),
    pytest.param("AuditedStateStore", True, id="4-audited-state-store"),
    pytest.param("WorkspaceManager", True, id="5-workspace-manager"),
    pytest.param("RunPersistence", True, id="6-run-persistence"),
]


@pytest.mark.parametrize(("failing", "operations_built"), _FAULT_CASES)
def test_failed_open_closes_built_stores_and_frees_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
    operations_built: bool,
) -> None:
    root = tmp_path / "runs"
    checkpoints: list[CheckpointStore] = []
    operations: list[OperationLedger] = []
    _record_real(monkeypatch, "CheckpointStore", CheckpointStore, checkpoints)
    _record_real(monkeypatch, "OperationLedger", OperationLedger, operations)
    monkeypatch.setattr(gp, failing, _boom)

    with pytest.raises(_InjectedOpenError, match="injected open failure"):
        gp.open_run_persistence(root)

    assert len(checkpoints) == 1
    _assert_checkpoints_closed(checkpoints[0])
    assert len(operations) == (1 if operations_built else 0)
    for ledger in operations:
        _assert_operations_closed(ledger)
    assert _locks_free(root)


def test_cleanup_close_error_does_not_replace_the_open_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 7: a close that raises during cleanup is fault injection, not a spy.

    The faked ``close()`` closes the real store, notes whether the locks were still
    held at that moment, then raises. The open's own error must still reach the
    caller, the other store must still be closed, and the locks must end up free.
    """

    root = tmp_path / "runs"
    held_during_close: list[bool] = []

    def build_checkpoints(*args: Any, **kwargs: Any) -> CheckpointStore:
        store = CheckpointStore(*args, **kwargs)
        real_close = store.close

        def failing_close() -> None:
            real_close()
            held_during_close.append(not _locks_free(root))
            raise _InjectedCloseError("injected close failure")

        monkeypatch.setattr(store, "close", failing_close)
        checkpoints.append(store)
        return store

    checkpoints: list[CheckpointStore] = []
    operations: list[OperationLedger] = []
    monkeypatch.setattr(gp, "CheckpointStore", build_checkpoints)
    _record_real(monkeypatch, "OperationLedger", OperationLedger, operations)
    monkeypatch.setattr(gp, "TrajectoryStore", _boom)

    with pytest.raises(_InjectedOpenError, match="injected open failure") as excinfo:
        gp.open_run_persistence(root)

    assert not isinstance(excinfo.value, _InjectedCloseError)
    assert held_during_close == [True]
    _assert_checkpoints_closed(checkpoints[0])
    assert len(operations) == 1
    _assert_operations_closed(operations[0])
    assert _locks_free(root)
