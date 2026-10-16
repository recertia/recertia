"""Atomic replace, write-once publish, and quarantine."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from recertia.faults import fault_point
from recertia.runtime.errors import StateCorrupt

if TYPE_CHECKING:
    from recertia.settings import Settings


class FileClass(StrEnum):
    FAIL_CLOSED = "fail_closed"
    QUARANTINE_REBUILD = "quarantine"
    WRITE_ONCE = "write_once"
    APPEND = "append"


@dataclass(frozen=True)
class Durability:
    fsync_file: bool
    fsync_dir: bool


def durability_from_settings(settings: Settings) -> Durability:
    if settings.durability == "fast":
        return Durability(fsync_file=False, fsync_dir=False)
    return Durability(fsync_file=True, fsync_dir=True)


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    durability: Durability,
    mode: int = 0o600,
) -> None:
    """Write via ``mkstemp`` in the same directory, then ``os.replace``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        os.write(fd, data)
        if durability.fsync_file:
            os.fsync(fd)
        os.fchmod(fd, mode)
        os.close(fd)
        fd = -1
        fault_point("atomic.before_replace")
        os.replace(tmp, path)
        if durability.fsync_dir:
            _fsync_dir(path.parent)
        fault_point("atomic.after_replace")
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_text(
    path: Path,
    text: str,
    *,
    durability: Durability,
    mode: int = 0o600,
) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), durability=durability, mode=mode)


def atomic_write_json(
    path: Path,
    payload: object,
    *,
    durability: Durability,
    mode: int = 0o600,
) -> None:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    atomic_write_text(path, text, durability=durability, mode=mode)


def quarantine_file(path: Path, *, quarantine_dir: Path, reason: str) -> Path | None:
    """Move ``path`` aside. Returns the quarantined path, or None if it was missing."""

    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dest_dir = quarantine_dir / stamp
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    data = path.read_bytes()
    os.replace(path, dest)
    reason_path = dest_dir / "reason.json"
    reason_path.write_text(
        json.dumps(
            {
                "error": reason,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return dest


def read_json_document(
    path: Path,
    *,
    file_class: FileClass,
    quarantine_dir: Path,
) -> object | None:
    """``None`` means missing or quarantined-rebuild. Fail-closed raises ``StateCorrupt``."""

    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        quarantine_file(path, quarantine_dir=quarantine_dir, reason=str(exc))
        if file_class is FileClass.FAIL_CLOSED:
            raise StateCorrupt(
                f"corrupt document: {path}",
                details={"path": str(path)},
            ) from exc
        return None
    return parsed


def write_once_bytes(
    path: Path,
    data: bytes,
    *,
    durability: Durability,
    mode: int = 0o600,
    expected_sha256: str | None = None,
    quarantine_dir: Path | None = None,
) -> bool:
    """Publish ``data`` once. True if this call wrote the file.

    A byte-identical existing file is a no-op. A short file is quarantined and
    republished once. A full file with different bytes raises ``StateCorrupt``.
    """

    digest = expected_sha256 or hashlib.sha256(data).hexdigest()
    qdir = quarantine_dir or (path.parent / "quarantine")

    def _matches(existing: Path) -> bool:
        blob = existing.read_bytes()
        if len(blob) != len(data):
            return False
        return hashlib.sha256(blob).hexdigest() == digest

    if path.exists():
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise StateCorrupt(f"unreadable write-once file: {path}", details={"path": str(path)}) from exc
        if size != len(data):
            quarantine_file(path, quarantine_dir=qdir, reason="torn write-once")
        elif _matches(path):
            return False
        else:
            raise StateCorrupt(
                f"write-once content mismatch: {path}",
                details={"path": str(path)},
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        os.write(fd, data)
        if durability.fsync_file:
            os.fsync(fd)
        os.fchmod(fd, mode)
        os.close(fd)
        fd = -1
        try:
            os.link(tmp, path)
            published = True
        except FileExistsError:
            published = False
        except OSError:
            os.replace(tmp, path)
            published = True
        if not published:
            if path.exists() and _matches(path):
                return False
            raise StateCorrupt(
                f"write-once content mismatch: {path}",
                details={"path": str(path)},
            )
        if durability.fsync_dir:
            _fsync_dir(path.parent)
        return True
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except OSError:
            pass
