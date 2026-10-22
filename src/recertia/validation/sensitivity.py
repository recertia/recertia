"""Sensitivity-proof authoring: execute a criterion against a known-bad fixture (specs §15)."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from contracts.criteria import (
    SensitivityProof,
    SkillCertificationCriterion,
    TaskCriterion,
    sensitivity_evidence_hash,
    sensitivity_proof_binds,
)

__all__ = [
    "author_sensitivity_proof",
    "empty_negative_fixture",
    "mutate_workspace",
    "sensitivity_evidence_hash",
    "sensitivity_proof_binds",
    "workspace_fingerprint",
]


def workspace_fingerprint(workdir: Path) -> str:
    """Stable hash of file paths + contents under ``workdir`` (for ``checked_against``)."""

    h = hashlib.sha256()
    if not workdir.exists():
        return h.hexdigest()
    for path in sorted(workdir.rglob("*")):
        if path.is_file() and not path.is_symlink():
            try:
                resolved = path.resolve()
                resolved.relative_to(workdir.resolve())
            except ValueError:
                continue
            rel = path.relative_to(workdir).as_posix()
            h.update(rel.encode())
            h.update(path.read_bytes())
    return h.hexdigest()


def author_sensitivity_proof(
    criterion: TaskCriterion | SkillCertificationCriterion,
    *,
    negative_workdir: Path | None = None,
    runner: Callable[[TaskCriterion | SkillCertificationCriterion, Path], bool] | None = None,
) -> SensitivityProof:
    """Run ``criterion`` against ``negative_workdir``; a vacuous criterion will not reject.

    Without ``negative_workdir`` the criterion runs in an empty ``recertia-neg-*``
    temporary directory that is removed before this returns or raises.
    """

    if negative_workdir is None:
        with tempfile.TemporaryDirectory(prefix="recertia-neg-") as tmp:
            return author_sensitivity_proof(criterion, negative_workdir=Path(tmp), runner=runner)
    runner = runner or _default_runner
    rejected = not runner(criterion, negative_workdir)
    negative_fingerprint = workspace_fingerprint(negative_workdir)
    return SensitivityProof(
        criterion_id=criterion.id,
        negative_fixture=str(negative_workdir),
        rejected=rejected,
        checked_at=datetime.now(timezone.utc),
        checked_against=f"sha256:{negative_fingerprint}",
        evidence_hash=sensitivity_evidence_hash(criterion, negative_fingerprint),
    )


def empty_negative_fixture(parent: Path | None = None) -> Path:
    """Create an empty temporary workspace; the caller must remove it.

    Prefer ``author_sensitivity_proof`` without ``negative_workdir``, which cleans up.
    """

    return Path(tempfile.mkdtemp(prefix="recertia-neg-", dir=parent))


def mutate_workspace(source: Path, *, drop_files: bool = True) -> Path:
    """Clone ``source`` then clear files so a real criterion should fail."""

    dest = Path(tempfile.mkdtemp(prefix="recertia-mut-"))
    if source.exists():
        for item in source.iterdir():
            target = dest / item.name
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
    if drop_files:
        for path in list(dest.rglob("*")):
            if path.is_file():
                path.unlink()
    return dest


def _default_runner(
    criterion: TaskCriterion | SkillCertificationCriterion, workdir: Path
) -> bool:
    """Run criteria through the same scorer ``validate`` uses."""

    if criterion.kind == "judge":
        # Judges cannot author a mechanical proof here — treat as not rejected.
        return True
    from recertia.nodes.validate import criterion_runner

    return criterion_runner().score(criterion, workdir=workdir).passed
