"""Workspace snapshot/offload integrity: rollback is byte-exact; tampered packs are refused.

Targets WorkspaceManager (src/recertia/workspace/snapshot.py) and WorkingSetOffload
(src/recertia/workspace/offload.py).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from recertia.workspace.offload import OffloadError, WorkingSetOffload
from recertia.workspace.snapshot import WorkspaceManager


def _digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _seed(wd: Path) -> None:
    (wd / "sub").mkdir(parents=True)
    (wd / "a.txt").write_text("alpha")
    (wd / "sub" / "b.json").write_text('{"k": 1}')


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: WorkspaceManager.restore skips files whose (size, mtime_ns) match the snapshot, so a "
        "same-size edit with reset mtime survives rollback. FIX: compare content hashes."
    ),
)
def test_restore_reverts_same_size_same_mtime_edit(tmp_path: Path) -> None:
    """restore() skips files whose (size, mtime_ns) match; an attempt that rewrites a file
    with same-length content and resets mtime must still be reverted."""
    wd = tmp_path / "ws"
    _seed(wd)
    wm = WorkspaceManager(tmp_path / "snaps")
    ref = wm.snapshot(wd, "run-1", 1)
    before = _digest(wd)
    st = (wd / "a.txt").stat()
    (wd / "a.txt").write_text("ALPHA")  # same size
    os.utime(wd / "a.txt", ns=(st.st_atime_ns, st.st_mtime_ns))
    (wd / "new.txt").write_text("junk")
    (wd / "sub" / "b.json").unlink()
    wm.restore(wd, ref)
    assert _digest(wd) == before


def test_offload_round_trip_and_tamper(tmp_path: Path) -> None:
    wd = tmp_path / "ws"
    _seed(wd)
    before = _digest(wd)
    off = WorkingSetOffload(tmp_path / "packs")
    h = off.pack(wd, ref="run-1")
    off.write_sidecar("run-1", h)
    assert not wd.exists()
    h2 = off.read_sidecar("run-1")
    assert h2 == h  # sidecar survives "restart"
    off.restore(h2, wd)
    assert _digest(wd) == before
    arch = tmp_path / "packs" / h.archive
    arch.write_bytes(arch.read_bytes()[:-8] + b"\0" * 8)
    with pytest.raises(OffloadError):
        off.restore(h, tmp_path / "ws2")
    assert not (tmp_path / "ws2").exists()
