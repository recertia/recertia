"""Per-run event log + SSE helpers (console C2)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from recertia.api.auth import validate_tenant_id


class RunEventLog:
    """Append-only JSONL events under ``{root}/runs/<tenant>/run_events/<run_id>.jsonl``.

    Every read and write is keyed by tenant. A run id that exists for another
    tenant is not visible here.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, tenant_id: str, run_id: str) -> Path:
        tenant_id = validate_tenant_id(tenant_id)
        safe = run_id.replace("/", "_").replace("..", "_")
        if not safe or safe in {".", ".."}:
            raise ValueError(f"invalid run_id: {run_id!r}")
        return self.root / "runs" / tenant_id / "run_events" / f"{safe}.jsonl"

    def exists(self, tenant_id: str, run_id: str) -> bool:
        return self._path(tenant_id, run_id).is_file()

    def append(
        self,
        tenant_id: str,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "event_id": uuid4().hex[:16],
            "run_id": run_id,
            "tenant_id": tenant_id,
            "type": event_type,
            "at": datetime.now(timezone.utc).isoformat(),
            "payload": payload or {},
        }
        with self._lock:
            path = self._path(tenant_id, run_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
        return event

    def list_after(
        self, tenant_id: str, run_id: str, *, after: str | None = None
    ) -> list[dict[str, Any]]:
        path = self._path(tenant_id, run_id)
        if not path.exists():
            return []
        out: list[dict[str, Any]] = []
        seen_after = after is None
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                if not seen_after:
                    if event.get("event_id") == after:
                        seen_after = True
                    continue
                out.append(event)
        return out

    def offset_after_event_id(self, tenant_id: str, run_id: str, event_id: str) -> int:
        """Byte offset exclusive of ``event_id``. Unknown ids start at 0."""

        path = self._path(tenant_id, run_id)
        if not path.exists():
            return 0
        pos = 0
        with path.open(encoding="utf-8") as handle:
            while True:
                line = handle.readline()
                if not line:
                    return pos
                if not line.endswith("\n"):
                    return pos
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    pos = handle.tell()
                    continue
                pos = handle.tell()
                if event.get("event_id") == event_id:
                    return pos
        return 0

    def follow(
        self,
        tenant_id: str,
        run_id: str,
        *,
        offset: int,
        timeout_s: float = 0.0,
        poll_s: float = 0.5,
        keepalive_s: float = 15.0,
        is_terminal: Callable[[], bool] | None = None,
    ) -> Iterator[str]:
        """Yield SSE from a byte offset. ``id`` is the exclusive end offset."""

        deadline = time.monotonic() + max(0.0, timeout_s)
        pos = max(0, int(offset))
        last_beat = time.monotonic()
        while True:
            events, pos = self._read_from(tenant_id, run_id, pos)
            for event, end in events:
                yield (
                    f"id: {end}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"
                )
            if is_terminal is not None and is_terminal():
                return
            now = time.monotonic()
            if now >= deadline:
                return
            if now - last_beat >= keepalive_s:
                yield ": keep-alive\n\n"
                last_beat = now
            remaining = deadline - now
            time.sleep(min(max(poll_s, 0.0), max(remaining, 0.0)))

    def _read_from(
        self, tenant_id: str, run_id: str, offset: int
    ) -> tuple[list[tuple[dict[str, Any], int]], int]:
        path = self._path(tenant_id, run_id)
        if not path.exists():
            return [], offset
        out: list[tuple[dict[str, Any], int]] = []
        with path.open(encoding="utf-8") as handle:
            handle.seek(max(0, offset))
            while True:
                start = handle.tell()
                line = handle.readline()
                if not line:
                    return out, start
                if not line.endswith("\n"):
                    return out, start
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    out.append((event, handle.tell()))
        return out, offset

    def iter_sse(self, tenant_id: str, run_id: str, *, after: str | None = None) -> Iterator[str]:
        """Yield SSE for the current backlog. ``after`` is an event id, exclusive."""

        offset = 0
        if after:
            offset = self.offset_after_event_id(tenant_id, run_id, after)
        yield from self.follow(tenant_id, run_id, offset=offset, timeout_s=0.0)
