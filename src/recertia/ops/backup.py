"""Backup and restore for the ``.recertia/`` durability unit (operator GA / RPO ≤ 24h)."""

from __future__ import annotations

import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class BackupError(ValueError):
    """Invalid backup or restore arguments."""


def default_archive_name(*, at: datetime | None = None) -> str:
    stamp = (at or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"recertia-{stamp}.tar.gz"


def online_backup_sqlite(source: Path, dest: Path) -> None:
    """Copy one SQLite database with the online backup API, including the WAL."""

    from recertia.persistence.db import open_db

    dest.parent.mkdir(parents=True, exist_ok=True)
    source_conn = open_db(source)
    try:
        dest_conn = open_db(dest)
        try:
            source_conn.backup(dest_conn)
            dest_conn.commit()
        finally:
            dest_conn.close()
    finally:
        source_conn.close()


def _is_sqlite_db(path: Path) -> bool:
    return path.suffix in {".sqlite", ".db"}


def _is_sqlite_sidecar(path: Path) -> bool:
    return path.name.endswith("-wal") or path.name.endswith("-shm")


def backup_tree(root: Path, archive: Path) -> Path:
    """Write a gzip tar of ``root`` (the whole ``.recertia/`` tree).

    SQLite databases are snapshotted with the backup API so a live WAL is
    included as one consistent file. ``-wal`` and ``-shm`` sidecars are omitted.
    """

    source = Path(root).resolve()
    dest = Path(archive).resolve()
    if not source.is_dir():
        raise BackupError(f"backup root does not exist: {source}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.relative_to(source)
    except ValueError:
        pass
    else:
        raise BackupError("archive path must not be inside the backup root")
    with tempfile.TemporaryDirectory() as tmp:
        snap_root = Path(tmp)
        with tarfile.open(dest, "w:gz") as tar:
            for path in sorted(source.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(source).as_posix()
                if _is_sqlite_sidecar(path):
                    continue
                if _is_sqlite_db(path) and not path.is_symlink():
                    snapshot = snap_root / rel
                    online_backup_sqlite(path, snapshot)
                    tar.add(snapshot, arcname=rel, recursive=False)
                    continue
                tar.add(path, arcname=rel, recursive=False)
    return dest


def restore_tree(archive: Path, dest: Path, *, overwrite: bool = False) -> Path:
    """Extract a backup archive into ``dest``. Refuses path-escaping members."""

    src = Path(archive).resolve()
    target = Path(dest).resolve()
    if not src.is_file():
        raise BackupError(f"archive not found: {src}")
    if target.exists() and any(target.iterdir()) and not overwrite:
        raise BackupError(f"restore dest is not empty: {target} (pass overwrite=True)")
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(src, "r:*") as tar:
        members: list[tarfile.TarInfo] = []
        for member in tar.getmembers():
            name = member.name.lstrip("/")
            if name not in {"", "."}:
                candidate = (target / name).resolve()
                try:
                    candidate.relative_to(target)
                except ValueError as exc:
                    raise BackupError(f"archive member escapes dest: {member.name}") from exc
            members.append(member)
        extract_kw: dict[str, Any] = {"path": target, "members": members}
        if hasattr(tarfile, "data_filter"):
            extract_kw["filter"] = "data"
        tar.extractall(**extract_kw)
    return target
