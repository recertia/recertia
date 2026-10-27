"""Backup and restore for the ``.recertia/`` durability unit (operator GA / RPO ≤ 24h)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recertia.persistence.layout import TENANT_LOCK_NAME, RuntimeLayout
from recertia.persistence.root_lock import RootLock, RootLockBusy

MANIFEST_NAME = "MANIFEST.json"
_MANIFEST_VERSION = 1
# The root's own lock files: empty lock targets that backup and restore hold
# exclusively while they run. They carry no data, so they are neither archived
# nor extracted. On Windows the locks are mandatory, so reading or overwriting a
# file through another handle while it is locked can fail (REC-058 MF-D).
_ROOT_LOCK_FILES = frozenset({".lock", "api.lock"})
# A runs root's tenant lock (REC-065), only where a runs root sits: the CLI layout's
# root itself, ``runs/<tenant>``, and the ``jobs`` runs root under either. A file of
# that name anywhere else (a snapshot, a workspace) is user content and is archived.
_TENANT_LOCK_RE = re.compile(r"(?:runs/[^/]+/)?(?:jobs/)?" + re.escape(TENANT_LOCK_NAME))


def _is_lock_file(rel: str) -> bool:
    """The root's ``.lock``/``api.lock``, or a runs root's tenant lock (REC-065).

    Tenant locks are empty too. Backup and restore take no tenant lock: the
    exclusive root ``.lock`` already keeps out every tenant holder, which takes
    the root lock shared first (lock order in ``recertia.persistence.root_lock``).
    """

    return rel in _ROOT_LOCK_FILES or _TENANT_LOCK_RE.fullmatch(rel) is not None


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
    included as one consistent file. ``-wal`` and ``-shm`` sidecars are omitted,
    and so are the root's ``.lock`` and ``api.lock``, which this call holds, and
    every runs root's empty ``.tenant.lock``.
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
    api_lock, lock = _exclusive_root_locks(source, action="backup")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            snap_root = Path(tmp)
            entries: list[dict[str, Any]] = []
            with tarfile.open(dest, "w:gz") as tar:
                for path in sorted(source.rglob("*")):
                    if not path.is_file() and not path.is_symlink():
                        continue
                    rel = path.relative_to(source).as_posix()
                    if rel == MANIFEST_NAME or _is_lock_file(rel) or _is_sqlite_sidecar(path):
                        continue
                    if _is_sqlite_db(path) and not path.is_symlink():
                        snapshot = snap_root / rel
                        online_backup_sqlite(path, snapshot)
                        _add_file(tar, snapshot, rel, entries)
                        continue
                    _add_file(tar, path, rel, entries)
                _add_manifest(tar, entries)
        return dest
    finally:
        lock.release()
        api_lock.release()


def restore_tree(archive: Path, dest: Path, *, overwrite: bool = False) -> Path:
    """Extract a backup archive into ``dest``. Refuses path-escaping members.

    The root's ``.lock`` and ``api.lock`` (held by this call) and a runs root's
    ``.tenant.lock`` (``.tenant.lock``, ``jobs/``, ``runs/<t>/`` and
    ``runs/<t>/jobs/``, matched after ``\\`` becomes ``/``) are not extracted, so an
    archive written before they were left out still restores. A file of that name
    anywhere else is user content and is extracted.
    """

    src = Path(archive).resolve()
    target = Path(dest).resolve()
    if not src.is_file():
        raise BackupError(f"archive not found: {src}")
    if target.exists() and any(target.iterdir()) and not overwrite:
        raise BackupError(f"restore dest is not empty: {target} (pass overwrite=True)")
    target.mkdir(parents=True, exist_ok=True)
    api_lock, lock = _exclusive_root_locks(target, action="restore")
    try:
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
                if _is_lock_file(_normalized_member(name)):
                    continue
                members.append(member)
            extract_kw: dict[str, Any] = {"path": target, "members": members}
            if hasattr(tarfile, "data_filter"):
                extract_kw["filter"] = "data"
            tar.extractall(**extract_kw)
        return target
    finally:
        lock.release()
        api_lock.release()


def _normalized_member(name: str) -> str:
    parts = [part for part in name.replace("\\", "/").split("/") if part not in {"", "."}]
    return "/".join(parts)


def verify_archive(archive: Path) -> dict[str, Any]:
    """Recompute sha256 for every manifest entry. Refuse mismatches and extras."""

    src = Path(archive).resolve()
    if not src.is_file():
        raise BackupError(f"archive not found: {src}")
    with tarfile.open(src, "r:*") as tar:
        try:
            manifest_info = tar.getmember(MANIFEST_NAME)
        except KeyError as exc:
            raise BackupError("archive has no MANIFEST.json") from exc
        raw = tar.extractfile(manifest_info)
        if raw is None:
            raise BackupError("archive has no MANIFEST.json")
        try:
            manifest = json.loads(raw.read().decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise BackupError("MANIFEST.json is not valid JSON") from exc
        if not isinstance(manifest, dict):
            raise BackupError("MANIFEST.json has an unsupported shape")
        files = manifest.get("files")
        if manifest.get("version") != _MANIFEST_VERSION or not isinstance(files, list):
            raise BackupError("MANIFEST.json has an unsupported shape")
        listed: dict[str, dict[str, Any]] = {}
        for entry in files:
            if not isinstance(entry, dict) or "path" not in entry:
                raise BackupError("MANIFEST.json entry is missing path")
            listed[str(entry["path"])] = entry
        present = {
            member.name
            for member in tar.getmembers()
            if member.isfile() or member.issym() or member.islnk()
        }
        extra = present - set(listed) - {MANIFEST_NAME}
        missing = set(listed) - present
        if extra or missing:
            detail = f"extra={sorted(extra)}, missing={sorted(missing)}"
            raise BackupError(f"archive members do not match MANIFEST.json ({detail})")
        for path, entry in listed.items():
            member = tar.getmember(path)
            digest, size = _member_digest(tar, member)
            if digest != entry.get("sha256") or size != entry.get("bytes"):
                raise BackupError(f"checksum mismatch: {path}")
    return manifest


def _exclusive_lock(path: Path, *, action: str, holder: str = "the API") -> RootLock:
    lock = RootLock(path)
    try:
        lock.try_exclusive()
    except RootLockBusy as exc:
        raise BackupError(f"{action} refused: {holder} holds the root lock") from exc
    return lock


def _exclusive_root_locks(root: Path, *, action: str) -> tuple[RootLock, RootLock]:
    """Exclusive on ``api.lock`` (the API presence lock) then ``.lock``.

    The exclusive ``.lock`` refuses while any tenant's import or run handle is
    live: each holds ``.lock`` shared under its tenant lock (REC-065 lock order).
    """

    layout = RuntimeLayout(root)
    api_lock = _exclusive_lock(layout.api_lock, action=action)
    try:
        root_lock = _exclusive_lock(
            layout.root_lock, action=action, holder="an open run handle, worker or import"
        )
    except Exception:
        api_lock.release()
        raise
    return api_lock, root_lock


def _add_file(tar: tarfile.TarFile, path: Path, arcname: str, entries: list[dict[str, Any]]) -> None:
    payload = _payload_bytes(path)
    entries.append(
        {"path": arcname, "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
    )
    tar.add(path, arcname=arcname, recursive=False)


def _add_manifest(tar: tarfile.TarFile, entries: list[dict[str, Any]]) -> None:
    body = _manifest_bytes(entries)
    info = tarfile.TarInfo(name=MANIFEST_NAME)
    info.size = len(body)
    tar.addfile(info, io.BytesIO(body))


def _manifest_bytes(entries: list[dict[str, Any]]) -> bytes:
    ordered = sorted(entries, key=lambda item: str(item["path"]))
    payload = {"version": _MANIFEST_VERSION, "files": ordered}
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _payload_bytes(path: Path) -> bytes:
    if path.is_symlink():
        return os.readlink(path).encode()
    return path.read_bytes()


def _member_digest(tar: tarfile.TarFile, member: tarfile.TarInfo) -> tuple[str, int]:
    if member.issym() or member.islnk():
        raw = (member.linkname or "").encode()
    else:
        extracted = tar.extractfile(member)
        raw = b"" if extracted is None else extracted.read()
    return hashlib.sha256(raw).hexdigest(), len(raw)
