"""REC-056-nits C1: every program route whose 2xx body has a top-level ``program`` sends
the program version as ``ETag``; error responses send none."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from tests.support.idempotency import close_app, headers_for, make_app, previewed_program, solved_run
from tests.unit.test_migration_programs import _init_git_repo, _step


class _Api:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.app = make_app(root)
        self.client = TestClient(self.app)
        self.headers = headers_for(self.app)

    def create(self, title: str = "pack", steps: list[dict[str, Any]] | None = None) -> str:
        resp = self.client.post(
            "/v1/programs", headers=self.headers, json={"title": title, "steps": steps or [_step("s1", 0)]}
        )
        assert_etag(resp)
        return str(resp.json()["program"]["program_id"])


@pytest.fixture
def api(tmp_path: Path) -> Iterator[_Api]:
    harness = _Api(tmp_path)
    try:
        yield harness
    finally:
        close_app(harness.app)


def assert_etag(resp: Any) -> None:
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("ETag") == f'"{resp.json()["program"]["version"]}"'


def assert_no_etag(resp: Any, status: int) -> None:
    assert resp.status_code == status, resp.text
    assert "ETag" not in resp.headers


def test_create_sends_etag(api: _Api) -> None:
    api.create()


def test_from_pack_sends_etag(api: _Api) -> None:
    resp = api.client.post(
        "/v1/programs/from-pack", headers=api.headers, json={"title": "from pack", "steps": [_step("s1", 0)]}
    )
    assert_etag(resp)


def test_accept_and_abandon_send_etag(api: _Api) -> None:
    pid = api.create()
    accepted = api.client.post(
        f"/v1/programs/{pid}/accept", headers=api.headers, json={"ack_disclaimer": True}
    )
    assert_etag(accepted)
    abandoned = api.client.post(f"/v1/programs/{pid}/abandon", headers=api.headers)
    assert_etag(abandoned)
    assert abandoned.headers["ETag"] != accepted.headers["ETag"]


def test_step_routes_send_etag(api: _Api) -> None:
    pid = api.create(steps=[_step("a", 0), _step("b", 1)])
    patched = api.client.patch(f"/v1/programs/{pid}/steps/b", headers=api.headers, json={"title": "b2"})
    assert_etag(patched)
    api.client.post(f"/v1/programs/{pid}/accept", headers=api.headers, json={"ack_disclaimer": True})
    skipped = api.client.post(f"/v1/programs/{pid}/steps/a/skip", headers=api.headers, json={"note": "done"})
    assert_etag(skipped)


def test_step_run_bind_sends_etag_and_plan_only_does_not(api: _Api) -> None:
    pid, ph = previewed_program(api.client, api.headers)
    planned = api.client.post(
        f"/v1/programs/{pid}/steps/s1/run", headers=api.headers, json={"plan_only": True}
    )
    assert planned.status_code == 200, planned.text
    assert "program" not in planned.json()
    assert "ETag" not in planned.headers
    api.app.state.runs[("t1", "run-1")] = solved_run("run-1", ph)
    bound = api.client.post(
        f"/v1/programs/{pid}/steps/s1/run",
        headers=api.headers,
        json={"bind_run_id": "run-1", "workdir": "ws", "idempotency_key": "k1"},
    )
    assert_etag(bound)
    replayed = api.client.post(
        f"/v1/programs/{pid}/steps/s1/run",
        headers=api.headers,
        json={"bind_run_id": "run-1", "workdir": "ws", "idempotency_key": "k1"},
    )
    assert_etag(replayed)
    assert replayed.headers["ETag"] == bound.headers["ETag"]


def test_repo_binding_and_record_tip_send_etag(api: _Api) -> None:
    _init_git_repo(api.root / "api-root" / "repo_bindings" / "t1" / "app")
    pid = api.create(steps=[_step("char", 0, role="characterization")])
    bound = api.client.post(
        f"/v1/programs/{pid}/repo-binding",
        headers=api.headers,
        json={"root": "app", "default_branch": "main"},
    )
    assert_etag(bound)
    api.client.post(f"/v1/programs/{pid}/accept", headers=api.headers, json={"ack_disclaimer": True})
    recorded = api.client.post(
        f"/v1/programs/{pid}/steps/char/record-tip", headers=api.headers, json={"use_binding_root": True}
    )
    assert_etag(recorded)


def test_error_responses_send_no_etag(api: _Api) -> None:
    missing = "p-missing"
    assert_no_etag(
        api.client.post(f"/v1/programs/{missing}/accept", headers=api.headers, json={"ack_disclaimer": True}),
        404,
    )
    assert_no_etag(api.client.post(f"/v1/programs/{missing}/abandon", headers=api.headers), 404)
    assert_no_etag(
        api.client.patch(f"/v1/programs/{missing}/steps/s1", headers=api.headers, json={"title": "x"}), 404
    )
    pid = api.create()
    assert_no_etag(
        api.client.post(f"/v1/programs/{pid}/steps/nope/skip", headers=api.headers, json={"note": "n"}), 409
    )
    assert_no_etag(
        api.client.post("/v1/programs/from-pack", headers=api.headers, json={"title": "bad", "steps": []}),
        422,
    )
