"""Per-run event log + SSE helpers (console C2)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from recertia.api.auth import validate_tenant_id
from recertia.api.event_follow import (
    EventFollower,
    check_offset,
    event_name,
    iter_follow,
    offset_after_event_id,
)


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

    def iter_sse(self, tenant_id: str, run_id: str, *, after: str | None = None) -> Iterator[str]:
        """Yield SSE chunks for current backlog (caller may poll again)."""

        events = self.list_after(tenant_id, run_id, after=after)
        last_id = after
        for event in events:
            last_id = event["event_id"]
            name = event_name(event.get("type"))
            yield f"id: {event['event_id']}\nevent: {name}\ndata: {json.dumps(event)}\n\n"
        if last_id:
            yield f": cursor {last_id}\n\n"

    def follower(self, tenant_id: str, run_id: str, *, after_offset: int = 0) -> EventFollower:
        """A cursor over complete events from ``after_offset``.

        Raises ``InvalidOffset`` unless the offset is 0 or the end of a logged event.
        """

        path = self._path(tenant_id, run_id)
        check_offset(path, after_offset)
        return EventFollower(path, after_offset)

    def offset_after(self, tenant_id: str, run_id: str, event_id: str) -> int | None:
        """Byte offset just past ``event_id``; ``None`` when this run has no such event."""

        return offset_after_event_id(self._path(tenant_id, run_id), event_id)

    def follow(
        self,
        tenant_id: str,
        run_id: str,
        *,
        after_offset: int = 0,
        max_seconds: float = 30.0,
        poll_s: float = 0.5,
        keepalive_s: float = 15.0,
    ) -> Iterator[str]:
        """Yield SSE frames from ``after_offset``, then newly appended ones.

        Each frame's ``id:`` is the byte offset just past its event. Ends once the log's
        last event is terminal (``run.finished`` or ``run.cancelled``) or after
        ``max_seconds``.
        """

        yield from iter_follow(
            self.follower(tenant_id, run_id, after_offset=after_offset),
            max_seconds=max_seconds,
            poll_s=poll_s,
            keepalive_s=keepalive_s,
        )
