"""F13: cancel is observed at the next hop boundary, not only before start."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from recertia.api import create_app
from recertia.graph.engine import GraphOrchestrator
from recertia.graph.hooks import Hop
from recertia.nodes.context import NodeOutcome


def test_cancel_running_run_stops_at_next_hop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue_holder: dict[str, Any] = {}
    orig = GraphOrchestrator._hop_after

    def after(
        self: GraphOrchestrator,
        hop: Hop,
        outcome: NodeOutcome,
        *,
        hop_ms: float,
        idle_gap_ms: float,
    ) -> Any:
        result = orig(self, hop, outcome, hop_ms=hop_ms, idle_gap_ms=idle_gap_ms)
        queue = queue_holder.get("queue")
        if queue is not None:
            queue.request_cancel("t1", hop.run_id)
        return result

    monkeypatch.setattr(GraphOrchestrator, "_hop_after", after)
    app = create_app(root=tmp_path / "root")
    queue_holder["queue"] = app.state.recertia.queue
    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    client = TestClient(app)
    created = client.post(
        "/v1/runs",
        headers={"X-API-Key": issued.secret},
        json={
            "request": "cancel me",
            "task_class": "repo-chore",
            "run_id": "cancel-hop",
            "mode": "sync",
            "budget": {"max_attempts": 1},
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body.get("terminal") == "cancelled" or body.get("status") == "cancelled"
    row = app.state.recertia.queue.get("t1", "cancel-hop")
    assert row is not None
    assert row.status == "cancelled"
