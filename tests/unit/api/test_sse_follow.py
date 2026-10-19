"""REL-6 F22: SSE follow from a byte offset."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from recertia.api.events import RunEventLog


def test_follow_emits_events_appended_after_connect(tmp_path: Path) -> None:
    log = RunEventLog(tmp_path)
    log.append("t1", "r1", "run.started", {"n": 1})
    chunks: list[str] = []

    def consume() -> None:
        for chunk in log.follow("t1", "r1", offset=0, timeout_s=2.0, poll_s=0.05):
            chunks.append(chunk)

    thread = threading.Thread(target=consume)
    thread.start()
    time.sleep(0.1)
    second = log.append("t1", "r1", "node.started", {"n": 2})
    thread.join(timeout=3)
    assert thread.is_alive() is False
    text = "".join(chunks)
    assert "run.started" in text
    assert second["event_id"] in text
    last_id = None
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("id: "):
                last_id = line[4:].strip()
    assert last_id is not None and last_id.isdigit()
    replay = "".join(log.follow("t1", "r1", offset=int(last_id), timeout_s=0.0))
    assert second["event_id"] not in replay
    assert "run.started" not in replay
