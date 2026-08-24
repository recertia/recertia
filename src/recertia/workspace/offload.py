"""Working-set pack / restore (ADR-0018). Content-addressed tarball, hash-checked.

Retention deletes; this parks. Eligible: snapshot directories and idle workdirs.
Approved skills, policy, and criteria are never packed here.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from recertia.paths import PathEscapeError, contained_path


class OffloadError(RuntimeError):
    """Pack or restore failed integrity or path checks."""


@dataclass(frozen=True)
class OffloadHandle:
    ref: str
    archive: str
    sha256: str
    bytes_offloaded: int
    offloaded_at: str
    original_bytes: int

    def as_dict(self) -> dict:
        return asdict(self)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for child in path.rglob("*"):
        if child.is_file() and not child.is_symlink():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total


class WorkingSetOffload:
    """Pack a directory to ``<packs_root>/<ref>.tar.gz``; restore verifies sha256."""

    def __init__(self, packs_root: Path | str) -> None:
        self.packs_root = Path(packs_root)
        self.packs_root.mkdir(parents=True, exist_ok=True)

    def sidecar_path(self, run_id: str) -> Path:
        return contained_path(self.packs_root, f"{run_id}.json")

    def write_sidecar(self, run_id: str, handle: OffloadHandle) -> Path:
        dest = self.sidecar_path(run_id)
        dest.write_text(json.dumps(handle.as_dict()) + "\n", encoding="utf-8")
        return dest

    def read_sidecar(self, run_id: str) -> OffloadHandle | None:
        dest = self.sidecar_path(run_id)
        if not dest.exists():
            return None
        return OffloadHandle(**json.loads(dest.read_text(encoding="utf-8")))

    def drop_sidecar(self, run_id: str) -> None:
        dest = self.sidecar_path(run_id)
        if dest.exists():
            dest.unlink()

    def pack(self, src: Path, *, ref: str) -> OffloadHandle:
        if not ref or "/" in ref or ".." in ref or "\\" in ref:
            raise PathEscapeError(f"invalid offload ref: {ref!r}")
        src = Path(src)
        if not src.exists():
            raise OffloadError(f"nothing to offload at {src}")
        archive = contained_path(self.packs_root, f"{ref}.tar.gz")
        original = _tree_bytes(src)
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(src, arcname="tree", recursive=True)
        digest = _sha256_file(archive)
        shutil.rmtree(src)
        return OffloadHandle(
            ref=ref,
            archive=archive.name,
            sha256=digest,
            bytes_offloaded=archive.stat().st_size,
            original_bytes=original,
            offloaded_at=datetime.now(timezone.utc).isoformat(),
        )

    def restore(self, handle: OffloadHandle, dest: Path) -> None:
        archive = contained_path(self.packs_root, handle.archive)
        if not archive.exists():
            raise OffloadError(f"offload archive missing: {handle.archive}")
        digest = _sha256_file(archive)
        if digest != handle.sha256:
            raise OffloadError(
                f"offload hash mismatch for {handle.ref}: expected {handle.sha256}, got {digest}"
            )
        dest = Path(dest)
        staging = dest.parent / f".restore-{handle.ref}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        extract_kw: dict = {"path": staging}
        if hasattr(tarfile, "data_filter"):
            extract_kw["filter"] = "data"
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(**extract_kw)
        extracted = staging / "tree"
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        extracted.rename(dest)
        shutil.rmtree(staging, ignore_errors=True)

    def drop_archive(self, handle: OffloadHandle) -> None:
        archive = contained_path(self.packs_root, handle.archive)
        if archive.exists():
            archive.unlink()
