"""Authoring-source inverted index + async revoke queue (ADR-0015).

The WAL (``lineage.jsonl``) is append-only audit. Lookups hit ``lineage.idx.json``,
a point map ``source → [skill_id@version, …]``. Rebuild from ``SkillVersion.provenance``.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from contracts.skill import SkillVersion
from contracts.status import SkillStatus
from recertia.memory.procedural.store import SkillStore


def source_keys(version: SkillVersion) -> list[str]:
    """Authoring keys plus the version's own skill identity."""

    keys = (
        [f"case:{i}" for i in version.provenance.source_case_ids]
        + [f"run:{i}" for i in version.provenance.source_run_ids]
        + [f"session:{i}" for i in version.provenance.source_session_ids]
        + [f"contributor:{i}" for i in version.provenance.source_contributor_ids]
        + [f"skill:{version.skill_id}@{version.version}"]
    )
    return keys


def _parse_target(target: str) -> tuple[str, int] | None:
    skill_id, sep, ver = str(target).partition("@")
    if not sep or not skill_id:
        return None
    try:
        return skill_id, int(ver)
    except ValueError:
        return None


class LineageIndex:
    """``source_id → [(skill_id, version)]``. Point lookup via the idx map."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.idx_path = self.path.with_name("lineage.idx.json")
        self._lock = threading.Lock()
        self._map: dict[str, list[str]] | None = None

    def _load_map(self) -> dict[str, list[str]]:
        if self._map is not None:
            return self._map
        if self.idx_path.exists():
            raw = json.loads(self.idx_path.read_text(encoding="utf-8"))
            self._map = {str(k): [str(t) for t in v] for k, v in raw.items()}
            return self._map
        self._map = self._replay_wal()
        if self._map:
            self._flush_idx()
        return self._map

    def _replay_wal(self) -> dict[str, list[str]]:
        mapping: dict[str, list[str]] = {}
        if not self.path.exists():
            return mapping
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            source = str(row.get("source") or "")
            target = str(row.get("target") or "")
            if not source or not target:
                continue
            bucket = mapping.setdefault(source, [])
            if target not in bucket:
                bucket.append(target)
        return mapping

    def _flush_idx(self) -> None:
        assert self._map is not None
        tmp = self.idx_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._map, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.idx_path)

    def record(self, version: SkillVersion) -> None:
        keys = source_keys(version)
        if not keys:
            return
        target = f"{version.skill_id}@{version.version}"
        with self._lock:
            mapping = self._load_map()
            changed = False
            with self.path.open("a", encoding="utf-8") as fh:
                for key in keys:
                    fh.write(json.dumps({"source": key, "target": target}) + "\n")
                    bucket = mapping.setdefault(key, [])
                    if target not in bucket:
                        bucket.append(target)
                        changed = True
            if changed:
                self._flush_idx()

    def lookup(self, source_kind: str, source_id: str) -> list[tuple[str, int]]:
        needle = f"{source_kind}:{source_id}"
        with self._lock:
            mapping = self._load_map()
            found: list[tuple[str, int]] = []
            for target in mapping.get(needle, []):
                parsed = _parse_target(target)
                if parsed is not None:
                    found.append(parsed)
            return found

    def rebuild(self, store: SkillStore) -> int:
        """Rebuild the idx (and compact WAL) from persisted versions. T0."""

        with self._lock:
            mapping: dict[str, list[str]] = {}
            counted = 0
            for version, _status, _stats in store.iter_loaded():
                keys = source_keys(version)
                if not keys:
                    continue
                target = f"{version.skill_id}@{version.version}"
                counted += 1
                for key in keys:
                    bucket = mapping.setdefault(key, [])
                    if target not in bucket:
                        bucket.append(target)
            self._map = mapping
            self._flush_idx()
            lines = [
                json.dumps({"source": source, "target": target})
                for source, targets in mapping.items()
                for target in targets
            ]
            self.path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
            return counted


class RevokeQueue:
    """Task-plane enqueues; Recertifier drains. ``record_dead_end`` stays O(1)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def enqueue(self, *, source_kind: str, source_id: str, reason: str) -> None:
        item = {
            "source_kind": source_kind,
            "source_id": source_id,
            "reason": reason,
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
        }
        from recertia.fsutil import JsonlAppender, durability_from_settings
        from recertia.settings import load_settings

        line = (json.dumps(item) + "\n").encode("utf-8")
        with self._lock:
            JsonlAppender(
                self.path,
                durability=durability_from_settings(load_settings()),
                fsync_each=True,
            ).append(line)

    def drain(self, limit: int) -> list[dict]:
        if not self.path.exists() or limit <= 0:
            return []
        from recertia.fsutil import JsonlRewriter, durability_from_settings
        from recertia.settings import load_settings

        with self._lock:
            lines = [ln for ln in self.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            taken = lines[:limit]
            rest = lines[limit:]
            JsonlRewriter().rewrite(
                self.path,
                [ln.encode("utf-8") for ln in rest],
                durability=durability_from_settings(load_settings()),
            )
        return [json.loads(ln) for ln in taken]


@dataclass
class LineageServices:
    index: LineageIndex
    queue: RevokeQueue

    @classmethod
    def open(cls, root: Path | str) -> "LineageServices":
        root_path = Path(root)
        return cls(
            index=LineageIndex(root_path / "lineage.jsonl"),
            queue=RevokeQueue(root_path / "revoke.jsonl"),
        )


def enqueue_revoke_for_quarantine(
    queue: RevokeQueue,
    version: SkillVersion,
    *,
    reason: str = "quarantined",
) -> None:
    """Enqueue the version and its authoring sources. Recertifier drains later."""

    queue.enqueue(
        source_kind="skill",
        source_id=f"{version.skill_id}@{version.version}",
        reason=reason,
    )
    for kind, ids in (
        ("run", version.provenance.source_run_ids),
        ("case", version.provenance.source_case_ids),
        ("session", version.provenance.source_session_ids),
        ("contributor", version.provenance.source_contributor_ids),
    ):
        for source_id in ids:
            queue.enqueue(source_kind=kind, source_id=source_id, reason=reason)


def _mark_needs_recert(store: SkillStore, skill_id: str, version: int) -> SkillStatus | None:
    try:
        status = store.get_status(skill_id, version)
    except FileNotFoundError:
        return None
    if status.lifecycle in ("quarantined", "deprecated", "needs_recert"):
        return None
    new_status = status.model_copy(update={"lifecycle": "needs_recert", "active": False})
    store.write_status(new_status)
    return new_status


def drain_revokes(
    store: SkillStore,
    index: LineageIndex,
    queue: RevokeQueue,
    *,
    max_writes: int = 50,
) -> list[SkillStatus]:
    """Mark intersecting versions and pinning parents ``needs_recert``. Caps writes per tick."""

    touched: list[SkillStatus] = []
    remaining = max_writes
    items = queue.drain(limit=max_writes)
    leftover: list[dict] = []
    parents_of: dict[tuple[str, int], list[tuple[str, int]]] = {}
    for parent_doc, _status, _stats in store.iter_loaded():
        for use in parent_doc.uses:
            parents_of.setdefault((use.skill_id, use.version), []).append(
                (parent_doc.skill_id, parent_doc.version)
            )
    for item in items:
        if remaining <= 0:
            leftover.append(item)
            continue
        targets = index.lookup(item["source_kind"], item["source_id"])
        for skill_id, ver_n in targets:
            if remaining <= 0:
                leftover.append(item)
                break
            marked = _mark_needs_recert(store, skill_id, ver_n)
            if marked is not None:
                touched.append(marked)
                remaining -= 1
            for parent_id, parent_n in parents_of.get((skill_id, ver_n), []):
                if remaining <= 0:
                    break
                parent_marked = _mark_needs_recert(store, parent_id, parent_n)
                if parent_marked is not None:
                    touched.append(parent_marked)
                    remaining -= 1
    for item in leftover:
        queue.enqueue(
            source_kind=item["source_kind"],
            source_id=item["source_id"],
            reason=item.get("reason") or "requeued",
        )
    return touched
