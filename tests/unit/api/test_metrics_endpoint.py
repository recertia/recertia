"""GET /metrics serves the in-process Prometheus registry."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from recertia.api import create_app


def test_metrics_requires_scope_and_returns_a_sample(tmp_path: Path) -> None:
    app = create_app(root=tmp_path / "root")
    client = TestClient(app)
    missing = client.get("/metrics")
    assert missing.status_code == 401

    runs_only = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    forbidden = client.get("/metrics", headers={"X-API-Key": runs_only.secret})
    assert forbidden.status_code == 403

    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"metrics"}, actor="test")
    got = client.get("/metrics", headers={"X-API-Key": issued.secret})
    assert got.status_code == 200, got.text
    assert "text/plain" in got.headers["content-type"]
    lines = [line for line in got.text.splitlines() if line and not line.startswith("#")]
    assert any(line.startswith("recertia_") for line in lines)
