"""Free-disk check and live-run snapshot retention (F30)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from recertia.errors import DiskSpaceLow
from recertia.retention import garbage_collect
from recertia.workspace.snapshot import WorkspaceManager


def test_low_free_space_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "blob.bin").write_bytes(b"x" * 4096)
    snapshots = tmp_path / "snapshots"

    class _Stat:
        f_bavail = 1
        f_frsize = 1

    monkeypatch.setattr(os, "statvfs", lambda _path: _Stat())
    manager = WorkspaceManager(snapshots)
    with pytest.raises(DiskSpaceLow):
        manager.snapshot(workdir, "run-a", 1)
    assert list(snapshots.iterdir()) == []


def test_gc_skips_snapshots_of_live_runs(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    snaps = root / "snapshots"
    snaps.mkdir(parents=True)
    live = snaps / "run-live-attempt1-aaaa"
    dead = snaps / "run-dead-attempt1-bbbb"
    live.mkdir()
    dead.mkdir()
    old = time.time() - (40 * 86400)
    os.utime(live, (old, old))
    os.utime(dead, (old, old))
    report = garbage_collect(root, older_than_days=14, live_run_ids={"run-live"})
    assert report.snapshots == ["run-dead-attempt1-bbbb"]
    assert report.skipped_live == ["run-live-attempt1-aaaa"]
    assert live.is_dir()
    assert not dead.exists()
