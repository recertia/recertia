"""AgentSysBench-shaped systems metrics (ADR-0018).

Component class, RSS / workdir gauges, tool-key canonicalisation, and a six-property
snapshot. Telemetry emission stays on the engine and tool runtime; this module is
pure so tests do not need a live run.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

ComponentClass = Literal["llm", "retrieve", "validate", "container", "control_plane", "other"]

NODE_COMPONENT: dict[str, ComponentClass] = {
    "retrieve": "retrieve",
    "validate": "validate",
    "review": "validate",
    "solve": "container",
    "solve_apply": "container",
    "solve_script": "container",
    "solve_scratch": "llm",
    "solve_branches": "container",
    "plan": "llm",
    "distill": "llm",
    "evolve": "llm",
    "classify_failure": "llm",
    "intake": "control_plane",
    "context": "control_plane",
    "attempt": "control_plane",
    "store": "control_plane",
    "finalize": "control_plane",
    "fan_out": "control_plane",
    "join": "control_plane",
    "record_dead_end": "control_plane",
    "reject_draft": "control_plane",
    "guide_stitch": "control_plane",
}

CONTROL_PLANE_NODES = frozenset(
    name for name, klass in NODE_COMPONENT.items() if klass == "control_plane"
)
SOLVE_NODES = frozenset(
    {"solve", "solve_apply", "solve_script", "solve_scratch", "solve_branches"}
)


def component_class(node_name: str) -> ComponentClass:
    return NODE_COMPONENT.get(node_name, "other")


def rss_bytes() -> int:
    """Current process RSS in bytes. 0 when the platform does not expose it."""

    try:
        with open("/proc/self/statm", encoding="utf-8") as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        pass
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KiB; macOS reports bytes.
        return int(usage * 1024) if usage < 10**9 else int(usage)
    except (OSError, ValueError):
        return 0


def workdir_bytes(path: Path | str | None) -> int:
    """Apparent size of a workdir tree, skipping outbound symlinks."""

    if path is None:
        return 0
    root = Path(path)
    if not root.exists():
        return 0
    total = 0
    if root.is_file():
        try:
            return root.stat().st_size
        except OSError:
            return 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not (base / d).is_symlink()]
        for name in filenames:
            fp = base / name
            if fp.is_symlink():
                continue
            try:
                total += fp.stat().st_size
            except OSError:
                continue
    return total


def hop_finished_attrs(
    node_name: str,
    *,
    hop_ms: float,
    workdir: Path | str | None,
    idle_gap_ms: float,
    tokens: int,
) -> dict[str, Any]:
    """Gauges emitted on ``node.finished``. Engine must not inline RSS / workdir / idle."""

    return {
        "node": node_name,
        "component_class": component_class(node_name),
        "latency_ms": round(hop_ms, 3),
        "rss_bytes": rss_bytes(),
        "workdir_bytes": workdir_bytes(workdir),
        "idle_gap_ms": round(idle_gap_ms, 3),
        "tokens": int(tokens),
    }


NOT_ESTABLISHED = "not established"


def not_established_detail(reason: str) -> str:
    """Honest lift/status language. Never a 4.6× or established-lift claim from fixtures."""

    text = reason.strip()
    if text.lower().startswith(NOT_ESTABLISHED):
        return text
    return f"{NOT_ESTABLISHED}: {text}"


def canonical_args_hash(inputs: Mapping[str, Any] | None) -> str:
    """Stable hash of tool/retrieve args so exact-match redundancy is countable."""

    payload = json.dumps(inputs or {}, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def canonical_tool_key(tool_name: str, inputs: Mapping[str, Any] | None, snapshot_hash: str = "") -> str:
    return f"{tool_name}:{canonical_args_hash(inputs)}:{snapshot_hash}"


def snapshot_stat_hash(path: Path | str | None) -> str:
    """Cheap content identity: relative path + size + mtime_ns. Not a cryptographic tree hash."""

    if path is None:
        return "none"
    root = Path(path)
    if not root.exists():
        return "missing"
    if root.is_file():
        st = root.stat()
        return hashlib.sha256(f"{root.name}:{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:16]
    rows: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not (base / d).is_symlink())
        rel_root = "." if base == root else str(base.relative_to(root))
        for name in sorted(filenames):
            fp = base / name
            if fp.is_symlink():
                continue
            try:
                st = fp.stat()
            except OSError:
                continue
            digest = hashlib.sha256()
            try:
                with fp.open("rb") as fh:
                    digest.update(fh.read(65536))
            except OSError:
                digest.update(b"")
            rows.append(
                f"{rel_root}/{name}:{st.st_size}:{st.st_mtime_ns}:{digest.hexdigest()[:12]}"
            )
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()[:16]


def redundancy_rate(keys: Sequence[str]) -> float:
    """Fraction of calls that repeat an earlier exact key. 0 when fewer than two calls."""

    if len(keys) < 2:
        return 0.0
    seen: set[str] = set()
    repeats = 0
    for key in keys:
        if key in seen:
            repeats += 1
        else:
            seen.add(key)
    return repeats / len(keys)


@dataclass
class SixPropertySnapshot:
    """AgentSysBench's six properties, Recertia-shaped. Targets are unset until a baseline exists."""

    llm_latency_ms: float = 0.0
    non_llm_latency_ms: float = 0.0
    peak_rss_bytes: int = 0
    peak_workdir_bytes: int = 0
    hop_latency_ms: dict[str, list[float]] = field(default_factory=dict)
    idle_gap_ms: list[float] = field(default_factory=list)
    control_plane_tokens: int = 0
    solve_tokens: int = 0
    tool_keys: list[str] = field(default_factory=list)
    retrieve_keys: list[str] = field(default_factory=list)
    hop_count: int = 0

    @property
    def non_llm_share(self) -> float:
        total = self.llm_latency_ms + self.non_llm_latency_ms
        return self.non_llm_latency_ms / total if total else 0.0

    @property
    def control_plane_token_tax(self) -> float:
        total = self.control_plane_tokens + self.solve_tokens
        return self.control_plane_tokens / total if total else 0.0

    @property
    def tool_redundancy_rate(self) -> float:
        return redundancy_rate(self.tool_keys)

    @property
    def retrieve_redundancy_rate(self) -> float:
        return redundancy_rate(self.retrieve_keys)

    @property
    def median_idle_gap_ms(self) -> float:
        if not self.idle_gap_ms:
            return 0.0
        ordered = sorted(self.idle_gap_ms)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return float(ordered[mid])
        return (ordered[mid - 1] + ordered[mid]) / 2.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "llm_latency_ms": self.llm_latency_ms,
            "non_llm_latency_ms": self.non_llm_latency_ms,
            "non_llm_share": self.non_llm_share,
            "peak_rss_bytes": self.peak_rss_bytes,
            "peak_workdir_bytes": self.peak_workdir_bytes,
            "hop_count": self.hop_count,
            "median_idle_gap_ms": self.median_idle_gap_ms,
            "control_plane_token_tax": self.control_plane_token_tax,
            "tool_redundancy_rate": self.tool_redundancy_rate,
            "retrieve_redundancy_rate": self.retrieve_redundancy_rate,
            "hop_latency_ms": self.hop_latency_ms,
        }


def snapshot_from_events(events: Iterable[Any]) -> SixPropertySnapshot:
    """Fold telemetry events (SpanEvent or dicts with name/attributes) into the six properties."""

    snap = SixPropertySnapshot()
    for event in events:
        name = getattr(event, "name", None) or (event.get("name") if isinstance(event, dict) else None)
        attrs = getattr(event, "attributes", None)
        if attrs is None and isinstance(event, dict):
            attrs = event.get("attributes") or {}
        attrs = attrs or {}
        if name == "node.finished":
            node = str(attrs.get("node") or "")
            latency = float(attrs.get("latency_ms") or 0.0)
            klass = str(attrs.get("component_class") or component_class(node))
            snap.hop_count += 1
            snap.hop_latency_ms.setdefault(node, []).append(latency)
            if klass == "llm":
                snap.llm_latency_ms += latency
            else:
                snap.non_llm_latency_ms += latency
            snap.peak_rss_bytes = max(snap.peak_rss_bytes, int(attrs.get("rss_bytes") or 0))
            snap.peak_workdir_bytes = max(
                snap.peak_workdir_bytes, int(attrs.get("workdir_bytes") or 0)
            )
            gap = attrs.get("idle_gap_ms")
            if gap is not None:
                snap.idle_gap_ms.append(float(gap))
            tokens = int(attrs.get("tokens") or 0)
            if node in CONTROL_PLANE_NODES or klass == "control_plane":
                snap.control_plane_tokens += tokens
            if node in SOLVE_NODES:
                snap.solve_tokens += tokens
        elif name == "tool.invoked":
            key = attrs.get("canonical_key")
            if key:
                snap.tool_keys.append(str(key))
        elif name == "retrieve.queried":
            key = attrs.get("canonical_key")
            if key:
                snap.retrieve_keys.append(str(key))
        elif name == "model.completed":
            role = str(attrs.get("role") or "solver")
            tokens = int(attrs.get("tokens") or 0)
            if role in {"judge", "reviewer", "context"}:
                snap.control_plane_tokens += tokens
            else:
                snap.solve_tokens += tokens
    return snap
