"""Skill library store: immutable SkillVersion writes, mutable SkillStatus/SkillStats (ADR-0007).

Layout on disk (matches ``docs/archive/2026-Q3/implementation-plan.md`` repository layout)::

    skills/<skill_id>/v<N>/version.json   # immutable once written
    skills/<skill_id>/v<N>/status.json    # projected SkillStatus
    skills/<skill_id>/v<N>/stats.json     # derived SkillStats (T0)

The "git adapter" for M1 is the filesystem under a skills root that is expected to live in a
git-tracked tree: writes go through this class so immutability is enforced in code, not by
reviewer habit. A later milestone MAY wrap each write in ``git add``/``git commit`` without
changing this interface.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from contracts.skill import SkillVersion
from contracts.stats import SkillStats
from contracts.status import SkillStatus

if TYPE_CHECKING:
    from recertia.memory.procedural.lineage import LineageIndex, RevokeQueue


class ImmutabilityError(Exception):
    """Raised when a caller attempts to overwrite an existing ``SkillVersion``."""


class ApprovedLifecycleError(Exception):
    """Raised for gated lifecycle writes: demotion to candidate, or approved outside promote."""


# Lifecycles that ``write_candidate`` must not clobber — only explicit helpers may leave them.
_PROTECTED_FROM_CANDIDATE_DEMOTE = frozenset(
    {"approved", "quarantined", "shadow", "benched", "needs_recert", "deprecated"}
)


class LifecycleConflictError(Exception):
    """Raised when a compare-and-swap status write finds an unexpected lifecycle."""


class SkillStore:
    def __init__(
        self,
        skills_root: Path | str,
        *,
        lineage_index: LineageIndex | None = None,
        revoke_queue: RevokeQueue | None = None,
    ) -> None:
        self.root = Path(skills_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lineage_index = lineage_index
        self.revoke_queue = revoke_queue

    def version_dir(self, skill_id: str, version: int) -> Path:
        return self.root / skill_id / f"v{version}"

    def write_version(self, version: SkillVersion) -> Path:
        """Write an immutable ``version.json``. Refuses if the file already exists."""

        from recertia.memory.procedural.hygiene import stamp_lint_hash

        version = stamp_lint_hash(version)
        dest_dir = self.version_dir(version.skill_id, version.version)
        dest = dest_dir / "version.json"
        dest_dir.mkdir(parents=True, exist_ok=True)
        payload = version.model_dump_json(indent=2) + "\n"
        try:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ImmutabilityError(
                f"SkillVersion {version.skill_id}@v{version.version} already exists at {dest}; "
                "evolution produces version N+1, never a rewrite (ADR-0007)"
            ) from exc
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        from recertia.memory.procedural.seeds.signature import stamp_seed

        stamp_seed(dest, version.skill_id)
        record = getattr(self.lineage_index, "record", None)
        if callable(record):
            record(version)
        return dest

    def write_candidate(self, version: SkillVersion) -> SkillVersion:
        """Persist a reviewable candidate: version + ``candidate`` status + default stats.

        Writes ``version.json`` when missing (callers that already allocated via
        ``allocate_and_write`` skip that step). Always writes ``lifecycle=candidate``,
        ``active=False`` — never approved. Refuses to demote non-draft lifecycles
        (``approved``/``quarantined``/``shadow``/``benched``/``needs_recert``/``deprecated``);
        those transitions belong to explicit lifecycle helpers.
        """

        from recertia.memory.procedural.hygiene import stamp_lint_hash

        version = stamp_lint_hash(version)
        dest = self.version_dir(version.skill_id, version.version) / "version.json"
        if not dest.exists():
            self.write_version(version)
        existing = self._read_status_if_present(version.skill_id, version.version)
        if existing is not None and existing.lifecycle in _PROTECTED_FROM_CANDIDATE_DEMOTE:
            raise ApprovedLifecycleError(
                f"refusing to demote {version.skill_id}@v{version.version} "
                f"from {existing.lifecycle!r} to candidate; use explicit lifecycle helpers "
                f"(e.g. maybe_advance_shadow_to_candidate, restore_benched), not write_candidate"
            )
        self.write_status(
            SkillStatus(
                skill_id=version.skill_id,
                version=version.version,
                lifecycle="candidate",
                active=False,
            )
        )
        self.write_stats(SkillStats(skill_id=version.skill_id, version=version.version))
        return version

    def write_status(
        self,
        status: SkillStatus,
        *,
        expected_lifecycle: str | None = None,
    ) -> Path:
        """Write status. Transitions *into* ``approved`` are rejected (use promote path).

        Updates to an already-approved record (e.g. active-set toggles) are allowed.
        When ``expected_lifecycle`` is set, refuse the write if the on-disk lifecycle
        does not match — a compare-and-swap guard against lost quarantine/bench races.
        """

        dest_dir = self.version_dir(status.skill_id, status.version)
        if not (dest_dir / "version.json").exists():
            raise FileNotFoundError(
                f"cannot write status for {status.skill_id}@v{status.version}: version.json missing"
            )
        existing = self._read_status_if_present(status.skill_id, status.version)
        if expected_lifecycle is not None:
            current = existing.lifecycle if existing is not None else None
            if current != expected_lifecycle:
                raise LifecycleConflictError(
                    f"status CAS failed for {status.skill_id}@v{status.version}: "
                    f"expected lifecycle={expected_lifecycle!r}, found {current!r}"
                )
        if status.lifecycle == "approved":
            if existing is None or existing.lifecycle != "approved":
                raise ApprovedLifecycleError(
                    f"refusing to write lifecycle=approved for "
                    f"{status.skill_id}@v{status.version}; use promote_to_approved"
                )
        dest = self._write_status_unchecked(status)
        self._maybe_enqueue_revoke(status, previous=existing)
        return dest

    def _write_status_unchecked(self, status: SkillStatus) -> Path:
        """Persist status without the approved-lifecycle gate (promote / test seeder only)."""

        dest_dir = self.version_dir(status.skill_id, status.version)
        if not (dest_dir / "version.json").exists():
            raise FileNotFoundError(
                f"cannot write status for {status.skill_id}@v{status.version}: version.json missing"
            )
        dest = dest_dir / "status.json"
        dest.write_text(status.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return dest

    def _maybe_enqueue_revoke(
        self, status: SkillStatus, *, previous: SkillStatus | None
    ) -> None:
        if status.lifecycle != "quarantined":
            return
        if previous is not None and previous.lifecycle == "quarantined":
            return
        if self.revoke_queue is None:
            return
        try:
            version = self.get_version(status.skill_id, status.version)
        except FileNotFoundError:
            return
        from recertia.memory.procedural.lineage import enqueue_revoke_for_quarantine

        enqueue_revoke_for_quarantine(
            self.revoke_queue, version, reason="quarantine_version"
        )

    def _read_status_if_present(self, skill_id: str, version: int) -> SkillStatus | None:
        path = self.version_dir(skill_id, version) / "status.json"
        if not path.exists():
            return None
        return SkillStatus.model_validate_json(path.read_text(encoding="utf-8"))

    def write_stats(self, stats: SkillStats) -> Path:
        dest_dir = self.version_dir(stats.skill_id, stats.version)
        if not (dest_dir / "version.json").exists():
            raise FileNotFoundError(
                f"cannot write stats for {stats.skill_id}@v{stats.version}: version.json missing"
            )
        dest = dest_dir / "stats.json"
        dest.write_text(stats.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return dest

    def get_version(self, skill_id: str, version: int) -> SkillVersion:
        path = self.version_dir(skill_id, version) / "version.json"
        from recertia.memory.procedural.seeds.signature import verify_seed

        verify_seed(path, skill_id)
        return SkillVersion.model_validate_json(path.read_text(encoding="utf-8"))

    def get_status(self, skill_id: str, version: int) -> SkillStatus:
        path = self.version_dir(skill_id, version) / "status.json"
        return SkillStatus.model_validate_json(path.read_text(encoding="utf-8"))

    def get_stats(self, skill_id: str, version: int) -> SkillStats:
        path = self.version_dir(skill_id, version) / "stats.json"
        if not path.exists():
            return SkillStats(skill_id=skill_id, version=version)
        return SkillStats.model_validate_json(path.read_text(encoding="utf-8"))

    def list_versions(self) -> list[tuple[str, int]]:
        """Every ``(skill_id, version)`` that has a ``version.json`` under the root."""

        found: list[tuple[str, int]] = []
        if not self.root.exists():
            return found
        for skill_dir in sorted(self.root.iterdir()):
            if not skill_dir.is_dir():
                continue
            for version_dir in sorted(skill_dir.iterdir()):
                if version_dir.is_dir() and version_dir.name.startswith("v"):
                    try:
                        ver = int(version_dir.name[1:])
                    except ValueError:
                        continue
                    if (version_dir / "version.json").exists():
                        found.append((skill_dir.name, ver))
        return found

    def iter_loaded(self) -> list[tuple[SkillVersion, SkillStatus, SkillStats]]:
        out: list[tuple[SkillVersion, SkillStatus, SkillStats]] = []
        for skill_id, version in self.list_versions():
            ver = self.get_version(skill_id, version)
            status_path = self.version_dir(skill_id, version) / "status.json"
            if status_path.exists():
                status = self.get_status(skill_id, version)
            else:
                status = SkillStatus(skill_id=skill_id, version=version, lifecycle="draft")
            stats = self.get_stats(skill_id, version)
            out.append((ver, status, stats))
        return out

    def library_fingerprint(self) -> str:
        """A cheap stat-based fingerprint of every persisted skill file.

        Hashes ``(path, size, mtime_ns)`` for all JSON files under the root without
        reading any of them. Any write through this store changes at least one entry,
        so an index built from a library with the same fingerprint is guaranteed to be
        current — this is what lets startup skip a full index rebuild when nothing
        changed. Uses plain string paths: this runs on every bootstrap.
        """

        import hashlib

        entries: list[str] = []
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for name in filenames:
                if not name.endswith(".json"):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                entries.append(f"{full}:{st.st_size}:{st.st_mtime_ns}")
        entries.sort()
        return hashlib.sha256("\n".join(entries).encode()).hexdigest()[:16]

    def dump_index_manifest(self) -> dict:
        """A small fingerprint of the library contents for run manifests."""

        items = [
            {
                "skill_id": sid,
                "version": ver,
                "sha256": _file_sha(self.version_dir(sid, ver) / "version.json"),
            }
            for sid, ver in self.list_versions()
        ]
        return {"skills": items, "count": len(items)}


def _file_sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()
