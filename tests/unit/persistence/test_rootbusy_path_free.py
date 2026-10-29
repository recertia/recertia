"""A ``RootBusy`` message names what is busy, never a filesystem path.

The text reaches run events, queue rows, logs and API envelopes, so a lock path or a
runtime root path in it leaks the host layout. One test per raise site, plus the
``TenantLock.refused`` strings those sites embed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import recertia.graph.persistence as gp
from recertia.graph.persistence import open_run_persistence
from recertia.persistence.import_legacy import import_legacy_runtime
from recertia.persistence.layout import RuntimeLayout
from recertia.persistence.root_lock import RootBusy, RootLock, RootLockBusy, TenantLock


def _assert_path_free(exc: BaseException, root: Path, *paths: Path) -> None:
    texts = [str(exc)]
    message = getattr(exc, "message", None)
    if message is not None:
        texts.append(str(message))
    for text in texts:
        assert text, "a busy refusal must still say something"
        for needle in {str(root), root.name, ".lock", *(str(p) for p in paths), *(p.name for p in paths)}:
            assert needle not in text, (needle, text)
        assert "/" not in text and "\\" not in text, text


def _pending_root(root: Path) -> None:
    """A root with leftover (not yet imported) runtime files."""

    from tests.unit.persistence.test_root_lock_writers import _state

    p = open_run_persistence(root, migrate="off")
    try:
        p.checkpoints.save("old-run", 1, "n", None, _state("old-run"))
    finally:
        p.close()


def test_refused_text_names_the_lock_without_its_path(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path / "host-only-dir").tenant("tenant-a")
    layout.runs_root.mkdir(parents=True)
    root_lock = RuntimeLayout(tmp_path / "host-only-dir").root_lock
    tenant_holder = RootLock(layout.tenant_lock)
    tenant_holder.try_exclusive()
    try:
        lock = TenantLock(root_lock, layout.tenant_lock)
        with pytest.raises(RootLockBusy):
            lock.acquire(exclusive=False, timeout_s=0)
        assert lock.refused == "tenant lock is held"
    finally:
        tenant_holder.release()
    root_holder = RootLock(root_lock)
    root_holder.try_exclusive()
    try:
        lock = TenantLock(root_lock, layout.tenant_lock)
        with pytest.raises(RootLockBusy):
            lock.acquire(exclusive=False, timeout_s=0)
        assert lock.refused == "root lock is held"
    finally:
        root_holder.release()


def test_open_refused_by_the_shared_root_names_no_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """graph/persistence.py, runtime.db branch: the shared acquire is refused."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0")
    root = tmp_path / "host-only-dir"
    open_run_persistence(root).close()
    holder = RootLock(RuntimeLayout(root).root_lock)
    holder.try_exclusive()
    try:
        with pytest.raises(RootBusy, match="root lock is held") as info:
            open_run_persistence(root)
    finally:
        holder.release()
    _assert_path_free(info.value, root, RuntimeLayout(root).root_lock)


def test_open_refused_by_the_tenant_lock_names_no_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0")
    root = tmp_path / "host-only-dir"
    tdir = root / "runs" / "tenant-a"
    open_run_persistence(tdir, runtime_root=root).close()
    layout = RuntimeLayout(root).tenant("tenant-a")
    holder = RootLock(layout.tenant_lock)
    holder.try_exclusive()
    try:
        with pytest.raises(RootBusy, match="tenant lock is held") as info:
            open_run_persistence(tdir, runtime_root=root)
    finally:
        holder.release()
    _assert_path_free(info.value, root, layout.tenant_lock, RuntimeLayout(root).root_lock)


def test_open_leftover_branch_refused_by_the_shared_root_names_no_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """graph/persistence.py, migrate=off branch with leftover files: bounded and refused."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0")
    root = tmp_path / "host-only-dir"
    _pending_root(root)
    holder = RootLock(RuntimeLayout(root).root_lock)
    holder.try_exclusive()
    try:
        with pytest.raises(RootBusy, match="root lock is held") as info:
            open_run_persistence(root, migrate="off")
    finally:
        holder.release()
    _assert_path_free(info.value, root, RuntimeLayout(root).root_lock)


def test_open_leftover_files_appeared_names_no_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The re-check under the lock finds leftover files a restore brought back."""

    root = tmp_path / "host-only-dir"
    open_run_persistence(root).close()
    monkeypatch.setattr(gp, "legacy_sqlite_pending", lambda layout: True)
    with pytest.raises(RootBusy, match="leftover runtime files appeared") as info:
        open_run_persistence(root)
    _assert_path_free(info.value, root, RuntimeLayout(root).root_lock)


def test_open_root_imported_while_waiting_names_no_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """migrate=off decided "leftover files", then the lock showed an imported runtime.db."""

    root = tmp_path / "host-only-dir"
    _pending_root(root)
    calls: list[str] = []

    def flip(layout: object, mode: str) -> tuple[bool, Path | None]:
        calls.append(mode)
        return len(calls) > 1, None

    monkeypatch.setattr(gp, "_resolve_runtime_db", flip)
    with pytest.raises(RootBusy, match="imported while waiting") as info:
        open_run_persistence(root, migrate="off")
    _assert_path_free(info.value, root, RuntimeLayout(root).root_lock)


@pytest.mark.parametrize("held", ["root", "tenant"])
def test_import_refused_names_no_path(tmp_path: Path, held: str) -> None:
    """persistence/import_legacy.py: the exclusive import lock is refused."""

    root = tmp_path / "host-only-dir"
    _pending_root(root)
    layout = RuntimeLayout(root).local_runs()
    layout.tenant_lock.parent.mkdir(parents=True, exist_ok=True)
    path = layout.parent.root_lock if held == "root" else layout.tenant_lock
    holder = RootLock(path)
    holder.try_exclusive()
    try:
        with pytest.raises(RootBusy, match="cannot import leftover runtime files") as info:
            import_legacy_runtime(layout, timeout_s=0)
    finally:
        holder.release()
    _assert_path_free(info.value, root, layout.parent.root_lock, layout.tenant_lock)
