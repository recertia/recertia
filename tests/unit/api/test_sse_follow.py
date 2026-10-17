"""SSE follow resumes from a byte offset or Last-Event-ID."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from recertia.api import RunRecord, create_app
from recertia.api.events import RunEventLog


def test_byte_offset_skips_earlier_events(tmp_path: Path) -> None:
    log = RunEventLog(tmp_path)
    first = log.append("t1", "r1", "run.queued", {"n": 1})
    second = log.append("t1", "r1", "run.started", {"n": 2})
    path = log._path("t1", "r1")
    data = path.read_bytes()
    first_end = data.index(b"\n") + 1
    text = "".join(log.iter_sse("t1", "r1", offset=first_end, timeout_s=0))
    assert f"id: {first['event_id']}" not in text
    assert second["event_id"] in text
    assert f": offset {len(data)}" in text


def test_follow_yields_an_append_that_arrives_during_the_wait(tmp_path: Path) -> None:
    log = RunEventLog(tmp_path)
    log.append("t1", "r1", "run.queued", {})

    def later() -> None:
        time.sleep(0.05)
        log.append("t1", "r1", "run.finished", {"terminal": "solved"})

    threading.Thread(target=later, daemon=True).start()
    text = "".join(log.iter_sse("t1", "r1", timeout_s=0.6))
    assert "event: run.queued" in text
    assert "event: run.finished" in text


def test_last_event_id_header_resumes(tmp_path: Path) -> None:
    app = create_app(root=tmp_path / "root")
    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    events = app.state.console_ctx.events
    first = events.append("t1", "run-sse", "run.queued", {"n": 1})
    second = events.append("t1", "run-sse", "run.finished", {"n": 2})
    app.state.runs[("t1", "run-sse")] = RunRecord(
        run_id="run-sse",
        request="x",
        task_class="repo-chore",
        status="solved",
        created_at=datetime.now(timezone.utc),
        tenant_id="t1",
        terminal="solved",
    )
    client = TestClient(app)
    resumed = client.get(
        "/v1/runs/run-sse/events",
        headers={"X-API-Key": issued.secret, "Last-Event-ID": first["event_id"]},
    )
    assert resumed.status_code == 200, resumed.text
    assert f"id: {first['event_id']}" not in resumed.text
    assert second["event_id"] in resumed.text
    assert ": offset " in resumed.text
