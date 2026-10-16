"""GET /ready checks sqlite, disk, and an optional worker heartbeat."""

from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from recertia.api import create_app


def test_health_stays_ok_and_ready_reports_sqlite(tmp_path: Path) -> None:
    app = create_app(root=tmp_path / "api-root")
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/ready")
    assert ready.status_code == 200
    body = ready.json()
    assert body["status"] == "ok"
    assert body["sqlite"] == "ok"
    assert body["disk"] == "ok"
    assert body["worker"] == "absent"
    assert body["disk_free_bytes"] > 4096


def test_ready_echoes_request_id(tmp_path: Path) -> None:
    app = create_app(root=tmp_path / "api-root")
    client = TestClient(app)
    resp = client.get("/ready", headers={"X-Request-Id": "req-ready-1"})
    assert resp.headers["x-request-id"] == "req-ready-1"


def test_stale_worker_heartbeat_is_not_ready(tmp_path: Path) -> None:
    root = tmp_path / "api-root"
    app = create_app(root=root)
    beat = root / "worker.heartbeat"
    beat.write_text("1", encoding="utf-8")
    os.utime(beat, (time.time() - 120, time.time() - 120))
    resp = TestClient(app).get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["worker"] == "stale"
    assert body["sqlite"] == "ok"
