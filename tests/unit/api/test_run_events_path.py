"""F15: run events land under the tenant runs root, not root/run_events."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from recertia.api import create_app


def test_events_written_under_tenant_runs_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    app = create_app(root=root)
    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    client = TestClient(app)
    created = client.post(
        "/v1/runs",
        headers={"X-API-Key": issued.secret},
        json={
            "request": "event path",
            "task_class": "repo-chore",
            "run_id": "evt-1",
            "budget": {"max_attempts": 1},
        },
    )
    assert created.status_code in {200, 202}, created.text
    if created.status_code == 202:
        app.state.recertia.embedded.flush(timeout=30)
    path = root / "runs" / "t1" / "run_events" / "evt-1.jsonl"
    assert path.is_file()
    legacy = root / "run_events" / "evt-1.jsonl"
    assert not legacy.exists()
