"""Per-run event log + SSE helpers (console C2)."""

from __future__ import annotations

import json
import threading
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

    def iter_sse(self, tenant_id: str, run_id: str, *, after: str | None = None) -> Iterator[str]:
        """Yield SSE chunks for current backlog (caller may poll again)."""

        events = self.list_after(tenant_id, run_id, after=after)
        last_id = after
        for event in events:
            last_id = event["event_id"]
            yield f"id: {event['event_id']}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"
        if last_id:
            yield f": cursor {last_id}\n\n"


    def follow(
        self,
        tenant_id: str,
        run_id: str,
        *,
        after_offset: int = 0,
        max_seconds: float = 30.0,
        poll_s: float = 0.25,
    ) -> Iterator[str]:
        """Yield SSE from ``after_offset`` then newly appended events (REC-056g).

        ``after_offset`` is a byte offset into the event log. A mid-line offset
        is advanced to the next newline so a fragment is never parsed.
        Stops when the run is terminal or ``max_seconds`` elapses.
        """
        import time

        path = self._path(tenant_id, run_id)
        if not path.is_file():
            return
        deadline = time.monotonic() + max_seconds
        offset = max(0, after_offset)
        # Advance a mid-line offset to the next newline.
        data = path.read_bytes()
        if offset > len(data):
            offset = len(data)
        elif offset > 0 and data[offset - 1:offset] != b"\n":
            nl = data.find(b"\n", offset)
            offset = nl + 1 if nl != -1 else len(data)
        while time.monotonic() < deadline:
            data = path.read_bytes()
            if offset < len(data):
                chunk = data[offset:]
                offset = len(data)
                for line in chunk.splitlines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    yield f"id: {offset}\nevent: {event.get('type','')}\ndata: {json.dumps(event)}\n\n"
            # Terminal check: last event type ends the follow.
            if path.is_file():
                tail = path.read_bytes()[-200:]
                if b'"type": "terminal"' in tail or b'"terminal"' in tail:
                    break
            time.sleep(poll_s)
