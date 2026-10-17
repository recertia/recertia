"""Per-run event log + SSE helpers (console C2)."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
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

    def iter_sse(
        self,
        tenant_id: str,
        run_id: str,
        *,
        after: str | None = None,
        offset: int | None = None,
        timeout_s: float = 0.0,
        poll_s: float = 0.02,
    ) -> Iterator[str]:
        """Yield SSE from a byte offset or an exclusive event id, then follow.

        ``offset`` is a file position. ``after`` is an event id (the
        ``Last-Event-ID``). ``timeout_s`` bounds the follow; ``0`` returns the
        lines already on disk and stops. The tail comment carries the byte
        offset to resume from.
        """

        path = self._path(tenant_id, run_id)
        if offset is not None:
            pos = max(0, int(offset))
        elif after:
            pos = self._offset_after_event(path, after)
        else:
            pos = 0
        deadline = time.monotonic() + max(0.0, timeout_s)
        last_id = after
        while True:
            events, pos = self._read_events_from(path, pos)
            for event in events:
                last_id = str(event.get("event_id") or "")
                yield (
                    f"id: {event['event_id']}\n"
                    f"event: {event['type']}\n"
                    f"data: {json.dumps(event)}\n\n"
                )
            if time.monotonic() >= deadline:
                if last_id:
                    yield f": cursor {last_id}\n\n"
                yield f": offset {pos}\n\n"
                return
            time.sleep(poll_s)

    def _offset_after_event(self, path: Path, after: str) -> int:
        """Byte position just past the line whose ``event_id`` is ``after``.

        An unknown id sits at EOF so the backlog is not replayed.
        """

        if not path.is_file():
            return 0
        with path.open("rb") as handle:
            while True:
                line = handle.readline()
                if not line:
                    return handle.tell()
                text = line.strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if event.get("event_id") == after:
                    return handle.tell()
        return 0

    def _read_events_from(self, path: Path, pos: int) -> tuple[list[dict[str, Any]], int]:
        """Complete JSONL records at ``pos``. A partial tail stays unread."""

        if not path.is_file():
            return [], pos
        with path.open("rb") as handle:
            handle.seek(pos)
            data = handle.read()
        if not data:
            return [], pos
        if not data.endswith(b"\n"):
            cut = data.rfind(b"\n")
            if cut < 0:
                return [], pos
            data = data[: cut + 1]
        events: list[dict[str, Any]] = []
        for line in data.splitlines():
            if not line.strip():
                continue
            events.append(json.loads(line))
        return events, pos + len(data)
