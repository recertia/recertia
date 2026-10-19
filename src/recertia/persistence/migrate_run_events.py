"""Move legacy ``root/run_events/*.jsonl`` under ``root/runs/<tenant>/run_events/``.

A second call is a no-op once every legacy file has been moved or quarantined.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from recertia.persistence.import_legacy import checkpoint_db_candidates, checkpoint_has_run


@dataclass(frozen=True)
class MigrationReport:
    moved: tuple[str, ...]
    quarantined: tuple[str, ...]
    pending: int


def migrate_run_events(root: Path | str) -> MigrationReport:
    """Attach a legacy file when exactly one tenant owns the run.

    Ownership is a ``workdir.json`` or a checkpoint row under
    ``root/runs/<tenant>/``. Zero or several owners go to
    ``root/run_events.quarantine/`` and stay unserved.
    """

    root = Path(root)
    legacy = root / "run_events"
    if not legacy.is_dir():
        return MigrationReport(moved=(), quarantined=(), pending=0)

    moved: list[str] = []
    quarantined: list[str] = []
    for path in sorted(legacy.glob("*.jsonl")):
        run_id = path.stem
        owners = _owners(root, run_id)
        if len(owners) == 1:
            dest = root / "runs" / owners[0] / "run_events" / path.name
            if _place(path, dest):
                moved.append(run_id)
            else:
                quarantine = root / "run_events.quarantine" / path.name
                if path.exists() and _place(path, quarantine):
                    quarantined.append(run_id)
        else:
            quarantine = root / "run_events.quarantine" / path.name
            if _place(path, quarantine):
                quarantined.append(run_id)

    pending = len(list(legacy.glob("*.jsonl"))) if legacy.is_dir() else 0
    if pending == 0 and legacy.is_dir():
        _retire_legacy_dir(legacy)
    return MigrationReport(
        moved=tuple(moved), quarantined=tuple(quarantined), pending=pending
    )


def _owners(root: Path, run_id: str) -> list[str]:
    runs = root / "runs"
    if not runs.is_dir():
        return []
    found: list[str] = []
    for tenant_dir in sorted(path for path in runs.iterdir() if path.is_dir()):
        if (tenant_dir / run_id / "workdir.json").is_file():
            found.append(tenant_dir.name)
            continue
        if any(checkpoint_has_run(db, run_id) for db in checkpoint_db_candidates(tenant_dir)):
            found.append(tenant_dir.name)
    return found


def _place(src: Path, dest: Path) -> bool:
    """Move ``src`` to ``dest``. Identical dest deletes ``src``. A different dest is left."""

    if not src.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if dest.read_bytes() == src.read_bytes():
            src.unlink()
            return True
        return False
    src.replace(dest)
    return True


def _retire_legacy_dir(legacy: Path) -> None:
    """Rename a leftover legacy directory so readers never open it again."""

    try:
        remaining = [path for path in legacy.iterdir() if path.name not in {".", ".."}]
    except OSError:
        return
    if not remaining:
        try:
            legacy.rmdir()
        except OSError:
            return
        return
    retired = legacy.parent / "run_events.migrated"
    if retired.exists():
        return
    try:
        legacy.rename(retired)
    except OSError:
        return
