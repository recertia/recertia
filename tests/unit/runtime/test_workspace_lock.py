"""F11: exclusive workspace flock."""

from __future__ import annotations

from pathlib import Path

import pytest

from recertia.persistence.layout import RuntimeLayout
from recertia.runtime.errors import WorkspaceBusy
from recertia.runtime.workspace_lock import WorkspaceLock


def test_second_acquire_on_same_path_raises_while_first_held(tmp_path: Path) -> None:
    locks_dir = RuntimeLayout(tmp_path).locks_dir
    workdir = tmp_path / "workspaces" / "job-a"
    workdir.mkdir(parents=True)

    lock = WorkspaceLock(locks_dir)
    first = lock.acquire(workdir, owner_id="owner-a")
    try:
        with pytest.raises(WorkspaceBusy) as exc_info:
            lock.acquire(workdir, owner_id="owner-b")
        assert exc_info.value.code == "workspace_busy"
        assert exc_info.value.retryable is True
        assert exc_info.value.http_status == 409
    finally:
        first.release()

    # After release, a new owner can take the lock.
    second = lock.acquire(workdir, owner_id="owner-b")
    second.release()


def test_distinct_workdirs_do_not_contend(tmp_path: Path) -> None:
    locks_dir = RuntimeLayout(tmp_path).locks_dir
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    lock = WorkspaceLock(locks_dir)
    ha = lock.acquire(a, owner_id="a")
    hb = lock.acquire(b, owner_id="b")
    ha.release()
    hb.release()
