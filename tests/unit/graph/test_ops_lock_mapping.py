"""The operations-schema lock in ``OperationLedger``: what becomes ``RootBusy`` and what does not.

Only a refused bounded wait (``RootLockBusy``) is a retryable busy. Any other failure
to take the lock propagates as itself, a bad ``RECERTIA_IMPORT_LOCK_TIMEOUT_S`` falls back
to the 5 s default, and the busy message carries no filesystem path (it reaches API
responses and run events).
"""

from __future__ import annotations

import errno
import sqlite3
import time
from pathlib import Path

import pytest

from recertia.graph.ops import OperationLedger
from recertia.persistence.root_lock import RootBusy, RootLock, RootLockBusy

_ENV = "RECERTIA_IMPORT_LOCK_TIMEOUT_S"


def _tables(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_lock_path_that_is_a_directory_raises_the_underlying_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "0.2")
    (tmp_path / ".lock").mkdir()

    with pytest.raises(OSError) as info:
        OperationLedger(tmp_path / "operations.db")

    assert not isinstance(info.value, RootLockBusy)
    assert not isinstance(info.value, RootBusy)


def test_other_lock_failure_is_not_reported_as_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def full_disk(self: RootLock, *, timeout_s: float | None = None) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(RootLock, "acquire_exclusive", full_disk)

    with pytest.raises(OSError) as info:
        OperationLedger(tmp_path / "operations.db")

    assert info.value.errno == errno.ENOSPC
    assert not isinstance(info.value, RootBusy)


def test_bad_lock_timeout_env_falls_back_to_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "abc")
    seen: list[float | None] = []
    real = RootLock.acquire_exclusive

    def spy(self: RootLock, *, timeout_s: float | None = None) -> None:
        seen.append(timeout_s)
        real(self, timeout_s=timeout_s)

    monkeypatch.setattr(RootLock, "acquire_exclusive", spy)

    ledger = OperationLedger(tmp_path / "operations.db")
    ledger.close()

    assert seen == [5.0]


def test_busy_message_carries_no_filesystem_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "0.2")
    holder = RootLock(tmp_path / ".lock")
    holder.try_exclusive()
    try:
        with pytest.raises(RootBusy) as info:
            OperationLedger(tmp_path / "operations.db")
    finally:
        holder.release()

    for text in (str(info.value), info.value.message):
        assert str(tmp_path) not in text
        assert tmp_path.name not in text
        assert ".lock" not in text
    assert isinstance(info.value.__cause__, RootLockBusy)


def test_held_lock_still_yields_root_busy_within_the_bound_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "0.2")
    db = tmp_path / "operations.db"
    holder = RootLock(tmp_path / ".lock")
    holder.try_exclusive()
    try:
        start = time.monotonic()
        with pytest.raises(RootBusy):
            OperationLedger(db)
        elapsed = time.monotonic() - start
    finally:
        holder.release()

    assert 0.15 <= elapsed < 5.0
    assert "operations" not in _tables(db)
    OperationLedger(db).close()
    assert "operations" in _tables(db)
