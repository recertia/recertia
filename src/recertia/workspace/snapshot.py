"""Snapshot and restore a run's workspace between attempts (specs §17).

M0's isolation model: copy the live workspace directory tree into a content-addressed
snapshot directory before every attempt; ``evolve`` restores from the most recent snapshot
before re-dispatching to ``solve``, so every retry starts from a byte-identical clean state
regardless of what the previous attempt left behind — including a half-applied edit or a
partially-run tool sequence.

Security: snapshot/restore never follow symlinks that escape the source tree. Absolute
symlinks and outbound relative symlinks are skipped. ``run_id`` / ``snapshot_ref`` are
path-contained under the snapshots root.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from recertia.errors import DiskSpaceLow
from recertia.ids import InvalidIdError, validate_run_id
from recertia.paths import PathEscapeError, contained_path, is_within

_SNAPSHOT_HEADROOM = 1.2


class WorkspaceManager:
    """Owns the snapshot store for one run's attempts.

    Snapshots are plain directory copies, not git worktrees or content-addressed blobs — the
    cheapest mechanism that is still correct. A later milestone MAY swap in git worktrees
    without changing this class's interface (``snapshot`` / ``restore`` / ``snapshot_path``).

    ``restore`` mirrors the snapshot into the workdir instead of wiping and re-copying:
    files whose ``(size, mtime_ns)`` still match the snapshot are left untouched, so a
    rollback after a small edit costs a handful of writes rather than a full tree copy.
    Because snapshots are written with ``copy2`` (mtime-preserving), any file the attempt
    did not touch still carries the snapshot's exact stat and is skipped; anything written,
    created, or deleted by the attempt is reverted.
    """

    def __init__(self, snapshots_root: Path | str) -> None:
        self._snapshots_root = Path(snapshots_root)
        self._snapshots_root.mkdir(parents=True, exist_ok=True)

    def snapshot(self, workdir: Path, run_id: str, attempt_no: int) -> str:
        """Copy ``workdir`` into a new snapshot; return its ``snapshot_ref``."""

        run_id = validate_run_id(run_id)
        ref = f"{run_id}-attempt{attempt_no}-{uuid.uuid4().hex[:8]}"
        dest = contained_path(self._snapshots_root, ref)
        need = int(_apparent_size(workdir) * _SNAPSHOT_HEADROOM) if workdir.exists() else 0
        _check_free_space(self._snapshots_root, need)
        if workdir.exists():
            _copy_tree_contained(workdir, dest)
        else:
            dest.mkdir(parents=True, exist_ok=True)
        return ref

    def restore(self, workdir: Path, snapshot_ref: str) -> None:
        """Make ``workdir`` an exact mirror of ``snapshot_ref``, with minimal writes."""

        src = self.snapshot_path(snapshot_ref)
        if not src.exists():
            raise FileNotFoundError(
                f"snapshot {snapshot_ref!r} not found under {self._snapshots_root}"
            )
        if not workdir.exists():
            workdir.parent.mkdir(parents=True, exist_ok=True)
            _copy_tree_contained(src, workdir)
            return
        _sync_directory(src, workdir)

    def snapshot_path(self, snapshot_ref: str) -> Path:
        if not snapshot_ref or "/" in snapshot_ref or "\\" in snapshot_ref or ".." in snapshot_ref:
            raise PathEscapeError(f"invalid snapshot_ref: {snapshot_ref!r}")
        # snapshot refs are "{run_id}-attemptN-hex"; run_id portion must stay path-safe.
        run_part = snapshot_ref.split("-attempt", 1)[0]
        try:
            validate_run_id(run_part)
        except InvalidIdError as exc:
            raise PathEscapeError(f"invalid snapshot_ref: {snapshot_ref!r}") from exc
        return contained_path(self._snapshots_root, snapshot_ref)

    def gc(
        self,
        *,
        older_than_days: float = 14.0,
        dry_run: bool = False,
        live_run_ids: set[str] | None = None,
        skipped_live: list[str] | None = None,
    ) -> list[str]:
        """Delete snapshot directories whose mtime is older than ``older_than_days``.

        Returns the snapshot_ref values removed (or that would be removed when
        ``dry_run`` is true).
        """

        import time

        cutoff = time.time() - (older_than_days * 86400.0)
        live = live_run_ids or set()
        removed: list[str] = []
        if not self._snapshots_root.exists():
            return removed
        for child in sorted(self._snapshots_root.iterdir()):
            if not child.is_dir():
                continue
            try:
                mtime = child.stat().st_mtime
            except OSError:
                continue
            if mtime > cutoff:
                continue
            run_id = child.name.split("-attempt", 1)[0]
            if run_id in live:
                if skipped_live is not None:
                    skipped_live.append(child.name)
                continue
            removed.append(child.name)
            if not dry_run:
                shutil.rmtree(child, ignore_errors=True)
        return removed


def _apparent_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, dirnames, filenames in os.walk(path, followlinks=False):
        root_path = Path(root)
        dirnames[:] = [name for name in dirnames if not (root_path / name).is_symlink()]
        for name in filenames:
            src_file = root_path / name
            if src_file.is_symlink():
                continue
            try:
                total += src_file.stat().st_size
            except OSError:
                continue
    return total


def _check_free_space(dest_parent: Path, need_bytes: int) -> None:
    """Refuse the copy when the filesystem cannot hold ``need_bytes``."""

    dest_parent.mkdir(parents=True, exist_ok=True)
    stats = os.statvfs(dest_parent)
    free = int(stats.f_bavail) * int(stats.f_frsize)
    from recertia.observability.metrics import metrics

    metrics.set_gauge("recertia_disk_free_bytes", float(free))
    if free < need_bytes:
        raise DiskSpaceLow(
            f"snapshot needs {need_bytes} bytes but only {free} are free",
            details={"need_bytes": need_bytes, "free_bytes": free},
        )


def _copy_tree_contained(src: Path, dst: Path) -> None:
    """Copy ``src`` → ``dst`` without following outbound symlinks."""

    src = src.resolve()
    dst.mkdir(parents=True, exist_ok=True)
    for root, dirnames, filenames in os.walk(src, followlinks=False):
        root_path = Path(root)
        # Do not descend into symlinked directories.
        dirnames[:] = [d for d in dirnames if not (root_path / d).is_symlink()]
        rel_root = os.path.relpath(root, src)
        dest_root = dst if rel_root == "." else dst / rel_root
        dest_root.mkdir(parents=True, exist_ok=True)
        for name in dirnames:
            (dest_root / name).mkdir(parents=True, exist_ok=True)
        for name in filenames:
            src_file = root_path / name
            dest_file = dest_root / name
            if src_file.is_symlink():
                _copy_symlink_if_contained(src_file, dest_file, tree_root=src)
            elif src_file.is_file():
                shutil.copy2(src_file, dest_file)


def _copy_symlink_if_contained(src_link: Path, dest_link: Path, *, tree_root: Path) -> None:
    """Recreate a symlink only when its target resolves inside ``tree_root``."""

    target = os.readlink(src_link)
    # Absolute links always escape a workdir tree for our purposes.
    if os.path.isabs(target):
        return
    resolved = (src_link.parent / target).resolve()
    if not is_within(tree_root, resolved):
        return
    if dest_link.exists() or dest_link.is_symlink():
        dest_link.unlink()
    os.symlink(target, dest_link)


def _sync_directory(src: Path, dst: Path) -> None:
    """Mirror ``src`` into ``dst``: delete what src lacks, rewrite only changed files."""

    src = src.resolve()
    dst.mkdir(parents=True, exist_ok=True)

    src_dirs: set[str] = set()
    src_files: dict[str, Path] = {}
    src_links: dict[str, str] = {}
    for root, dirnames, filenames in os.walk(src, followlinks=False):
        root_path = Path(root)
        dirnames[:] = [d for d in dirnames if not (root_path / d).is_symlink()]
        rel_root = os.path.relpath(root, src)
        for name in dirnames:
            src_dirs.add(os.path.normpath(os.path.join(rel_root, name)))
        for name in filenames:
            rel = os.path.normpath(os.path.join(rel_root, name))
            path = root_path / name
            if path.is_symlink():
                target = os.readlink(path)
                if os.path.isabs(target):
                    continue
                resolved = (path.parent / target).resolve()
                if not is_within(src, resolved):
                    continue
                src_links[rel] = target
            elif path.is_file():
                src_files[rel] = path

    for root, dirnames, filenames in os.walk(dst, topdown=False, followlinks=False):
        rel_root = os.path.relpath(root, dst)
        for name in filenames:
            rel = os.path.normpath(os.path.join(rel_root, name))
            dst_file = Path(root) / name
            if rel not in src_files and rel not in src_links:
                dst_file.unlink(missing_ok=True)
            elif dst_file.is_symlink() and rel in src_files:
                dst_file.unlink(missing_ok=True)
        for name in dirnames:
            rel = os.path.normpath(os.path.join(rel_root, name))
            dst_dir = Path(root) / name
            if dst_dir.is_symlink():
                dst_dir.unlink()
            elif rel not in src_dirs:
                shutil.rmtree(dst_dir, ignore_errors=True)

    for rel in sorted(src_dirs):
        dir_path = dst / rel
        if dir_path.is_symlink():
            dir_path.unlink()
        dir_path.mkdir(parents=True, exist_ok=True)
    for rel, src_file in src_files.items():
        dst_file = dst / rel
        if _unchanged(src_file, dst_file):
            continue
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        if dst_file.is_symlink():
            dst_file.unlink()
        shutil.copy2(src_file, dst_file)
    for rel, link_target in src_links.items():
        dst_file = dst / rel
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        if dst_file.exists() or dst_file.is_symlink():
            if dst_file.is_symlink() and os.readlink(dst_file) == link_target:
                continue
            dst_file.unlink()
        os.symlink(link_target, dst_file)


def _unchanged(src_file: Path, dst_file: Path) -> bool:
    """Same size and mtime: the attempt left this file alone (snapshot copies keep mtimes)."""

    try:
        if dst_file.is_symlink():
            return False
        src_stat = src_file.stat()
        dst_stat = dst_file.stat()
    except OSError:
        return False
    return src_stat.st_size == dst_stat.st_size and src_stat.st_mtime_ns == dst_stat.st_mtime_ns
