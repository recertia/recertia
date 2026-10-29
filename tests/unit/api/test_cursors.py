"""``GET /v1/runs`` paging over HTTP, and the strict cursor codec behind it (issue #136)."""

from __future__ import annotations

import base64
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from recertia.api import create_app
from recertia.errors import RecertiaError
from recertia.graph.store import CheckpointStore
from recertia.persistence.layout import RuntimeLayout
from recertia.runtime.cursor import (
    MAX_CURSOR_CHARS,
    RunPosition,
    decode_cursor,
    decode_run_cursor,
    encode_cursor,
    encode_run_cursor,
)
from recertia.runtime.errors import InvalidCursor
from recertia.runtime.registry import RunRegistry

T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)


def _client(tmp_path: Path, tenant_id: str = "t1") -> tuple[TestClient, Any, dict[str, str], Path]:
    root = tmp_path / "root"
    app = create_app(root=root)
    issued = app.state.api_keys.issue(tenant_id=tenant_id, scopes={"runs"}, actor="test")
    headers = {"X-API-Key": issued.secret}
    return (
        TestClient(app),
        app,
        headers,
        RuntimeLayout(root).tenant(tenant_id).runtime_db.parent,
    )


def _reserve(runs_dir: Path, tenant_id: str, run_id: str, created_at: datetime) -> None:
    """Reserve through the file the API writes reservations to, then date the row."""

    runs_dir.mkdir(parents=True, exist_ok=True)
    registry = RunRegistry(RuntimeLayout(runs_dir.parent.parent).tenant(tenant_id).runtime_db)
    registry.reserve(tenant_id, run_id, request_hash=f"h-{run_id}")
    registry._conn.execute(
        "UPDATE runs SET created_at = ? WHERE tenant_id = ? AND run_id = ?",
        (created_at.isoformat(), tenant_id, run_id),
    )
    registry._conn.commit()
    registry.close()


def _summarize(runs_dir: Path, rows: list[tuple[str, datetime, str, str | None]]) -> None:
    runs_dir.mkdir(parents=True, exist_ok=True)
    CheckpointStore(runs_dir / "checkpoints.db").close()
    conn = sqlite3.connect(runs_dir / "checkpoints.db")
    stamp = "%Y-%m-%d %H:%M:%S"
    conn.executemany(
        "INSERT INTO run_summaries (run_id, status, terminal, updated_at, cost_usd, task_class,"
        " attempt_no, arm, created_at) VALUES (?, ?, ?, ?, 0, ?, 1, 'treatment', ?)",
        [
            (
                run_id,
                terminal or "running",
                terminal,
                when.strftime(stamp),
                task_class,
                when.strftime(stamp),
            )
            for run_id, when, task_class, terminal in rows
        ],
    )
    conn.commit()
    conn.close()


def _page(client: TestClient, headers: dict[str, str], **params: Any) -> dict[str, Any]:
    response = client.get("/v1/runs", headers=headers, params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _http_walk(client: TestClient, headers: dict[str, str], limit: int, **params: Any) -> list[str]:
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(100):
        query = {**params, "limit": limit, **({"cursor": cursor} if cursor else {})}
        body = _page(client, headers, **query)
        seen += [item["run_id"] for item in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            return seen
    raise AssertionError("cursor walk did not end")


def test_http_lists_runs_newest_first_with_opaque_cursor(tmp_path: Path) -> None:
    client, _app, headers, runs_dir = _client(tmp_path)
    _summarize(
        runs_dir,
        [(f"r{i}", T0 + timedelta(seconds=i), "repo-chore", "solved") for i in range(5)],
    )
    first = _page(client, headers, limit=2)
    assert [item["run_id"] for item in first["items"]] == ["r4", "r3"]
    cursor = first["next_cursor"]
    assert isinstance(cursor, str)
    assert decode_run_cursor(cursor) == RunPosition(T0 + timedelta(seconds=3), "r3")
    assert _http_walk(client, headers, 2) == ["r4", "r3", "r2", "r1", "r0"]
    assert _http_walk(client, headers, 5) == ["r4", "r3", "r2", "r1", "r0"]
    assert _page(client, headers, limit=5)["next_cursor"] is None


def test_http_lists_a_run_reserved_through_the_tenant_registry(tmp_path: Path) -> None:
    client, _app, headers, runs_dir = _client(tmp_path)
    _reserve(runs_dir, "t1", "accepted-1", T0)
    body = _page(client, headers)
    assert [(item["run_id"], item["status"]) for item in body["items"]] == [("accepted-1", "accepted")]


def test_http_registry_and_checkpoint_rows_merge_with_filters(tmp_path: Path) -> None:
    client, _app, headers, runs_dir = _client(tmp_path)
    _reserve(runs_dir, "t1", "queued", T0 + timedelta(seconds=30))
    _reserve(runs_dir, "t1", "finished", T0 + timedelta(seconds=20))
    _summarize(
        runs_dir,
        [
            ("finished", T0 + timedelta(seconds=20), "bug", "solved"),
            ("older", T0 + timedelta(seconds=10), "repo-chore", "unsolved"),
        ],
    )
    items = _page(client, headers)["items"]
    assert [(item["run_id"], item["status"]) for item in items] == [
        ("queued", "accepted"),
        ("finished", "solved"),
        ("older", "unsolved"),
    ]
    assert _http_walk(client, headers, 1, terminal="solved") == ["finished"]
    assert _http_walk(client, headers, 1, task_class="repo-chore") == ["older"]


def test_http_runs_are_scoped_to_the_calling_tenant(tmp_path: Path) -> None:
    client, app, headers, runs_dir = _client(tmp_path)
    other = app.state.api_keys.issue(tenant_id="t2", scopes={"runs"}, actor="test")
    _reserve(runs_dir, "t1", "one", T0)
    _summarize(runs_dir, [("one-cp", T0 + timedelta(seconds=1), "repo-chore", None)])
    foreign_dir = runs_dir.parent / "t2"
    _reserve(foreign_dir, "t2", "two", T0)
    assert _http_walk(client, headers, 10) == ["one-cp", "one"]
    assert _http_walk(client, {"X-API-Key": other.secret}, 10) == ["two"]


def test_http_rejects_a_malformed_cursor_with_400(tmp_path: Path) -> None:
    client, _app, headers, _runs_dir = _client(tmp_path)
    for bad in (
        "not-a-cursor",
        "e30",
        encode_cursor({"created_at": "x", "run_id": "r"}),
        "A" * 600,
    ):
        response = client.get("/v1/runs", headers=headers, params={"cursor": bad})
        assert response.status_code == 400, bad
        assert "invalid_cursor" in response.text
        assert bad not in response.text


def test_http_limit_is_bounded(tmp_path: Path) -> None:
    client, _app, headers, _runs_dir = _client(tmp_path)
    assert client.get("/v1/runs", headers=headers, params={"limit": 0}).status_code == 422
    assert client.get("/v1/runs", headers=headers, params={"limit": 101}).status_code == 422
    assert client.get("/v1/runs", headers=headers, params={"limit": 100}).status_code == 200


def test_http_created_run_is_listed(tmp_path: Path) -> None:
    client, app, headers, _runs_dir = _client(tmp_path)
    created = client.post(
        "/v1/runs",
        headers=headers,
        json={
            "request": "list me",
            "task_class": "repo-chore",
            "run_id": "lst-1",
            "budget": {"max_attempts": 1},
        },
    )
    assert created.status_code in {200, 202}, created.text
    if created.status_code == 202:
        app.state.recertia.embedded.flush(timeout=30)
    items = _page(client, headers)["items"]
    assert [item["run_id"] for item in items] == ["lst-1"]
    assert items[0]["tenant_id"] == "t1"


def test_cursor_round_trips() -> None:
    position = RunPosition(T0 + timedelta(microseconds=7), "run-1")
    assert decode_run_cursor(encode_run_cursor(position)) == position
    undated = RunPosition(None, "run-2")
    assert decode_run_cursor(encode_run_cursor(undated)) == undated
    offset = encode_cursor({"created_at": "2026-01-01T12:00:00+02:00", "run_id": "r"})
    assert decode_run_cursor(offset).created_at == T0


def _b64(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "!!!!",
        "not base64 at all",
        _b64("not json"),
        _b64("[]"),
        _b64("[" * 100_000 + "]" * 100_000),
        _b64('"text"'),
        _b64('{"created_at":null}'),
        _b64('{"created_at":null,"run_id":"r","extra":"x"}'),
        _b64('{"created_at":null,"run_id":5}'),
        _b64('{"created_at":["x"],"run_id":"r"}'),
        _b64('{"created_at":null,"run_id":""}'),
        _b64('{"created_at":null,"run_id":null}'),
        _b64('{"created_at":"2026-01-01T10:00:00","run_id":"r"}'),
        _b64('{"created_at":"yesterday","run_id":"r"}'),
        _b64('{"run_id":"r","created_at":null}'),
        _b64('{"created_at": null, "run_id": "r"}'),
        _b64('{"created_at":null,"run_id":"' + "r" * 300 + '"}'),
        encode_cursor({"created_at": None, "run_id": "r"}) + "=",
        "\u00e9" + encode_cursor({"created_at": None, "run_id": "r"}),
    ],
)
def test_malformed_cursors_raise_invalid_cursor(bad: str) -> None:
    with pytest.raises(InvalidCursor) as caught:
        decode_run_cursor(bad)
    assert caught.value.http_status == 400
    assert isinstance(caught.value, RecertiaError)


def test_cursor_length_limit_is_enforced_before_decoding() -> None:
    long_id = "r" * 400
    cursor = encode_cursor({"created_at": None, "run_id": long_id})
    assert len(cursor) > MAX_CURSOR_CHARS
    with pytest.raises(InvalidCursor):
        decode_cursor(cursor, expected_keys={"created_at", "run_id"})


def test_decode_cursor_requires_the_expected_keys() -> None:
    cursor = encode_cursor({"a": "1"})
    assert decode_cursor(cursor, expected_keys={"a"}) == {"a": "1"}
    with pytest.raises(InvalidCursor):
        decode_cursor(cursor, expected_keys={"b"})
    assert json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))) == {"a": "1"}
