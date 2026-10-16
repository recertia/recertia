"""Atomic replace fsyncs under normal and leaves the previous file on kill."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from recertia.faults import FaultInjected
from recertia.fsutil.atomic import Durability, atomic_write_text


def test_replace_is_atomic_under_kill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "doc.json"
    path.write_text('{"ok":true}\n', encoding="utf-8")
    monkeypatch.setenv("RECERTIA_ALLOW_FAULTS", "1")
    monkeypatch.setenv("RECERTIA_FAULTS", "atomic.before_replace:kill")
    with pytest.raises(FaultInjected):
        atomic_write_text(path, '{"ok":false}\n', durability=Durability(True, True))
    assert path.read_text(encoding="utf-8") == '{"ok":true}\n'
    assert list(tmp_path.glob("*.tmp")) == []


def test_dir_fsync_called_for_normal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    real = os.fsync

    def spy(fd: int) -> None:
        seen.append(fd)
        real(fd)

    monkeypatch.setattr(os, "fsync", spy)
    atomic_write_text(tmp_path / "a.txt", "a", durability=Durability(True, True))
    assert len(seen) >= 2
    seen.clear()
    atomic_write_text(tmp_path / "b.txt", "b", durability=Durability(False, False))
    assert seen == []
