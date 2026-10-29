"""``/v1/programs`` answers ``root_busy`` (503, retryable) while the runtime root is busy (#145).

The programs routes share one error mapper. It used to turn a ``RootBusy`` into a plain
``HTTPException``, so the envelope said ``unavailable`` with ``retryable: false``. It now
sends the envelope the ``/v1/runs`` routes and the app-level handler send.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from recertia.api import RunRecord, create_app
from recertia.app.programs import ProgramService
from recertia.persistence.import_legacy import legacy_sqlite_pending
from recertia.persistence.layout import RuntimeLayout
from recertia.persistence.root_lock import RootBusy, RootLock
from tests.unit.api.test_run_get_root_busy import _tenant_with_pending_import

_STEP = {
    "step_id": "s1",
    "ordinal": 0,
    "title": "s1",
    "role": "custom",
    "goal": {
        "desired": [{"id": "s1-d", "kind": "file_exists", "path": "README.md", "weight": 1.0}],
        "context": "context for s1",
        "task_class": "repo-chore",
    },
}


def _assert_root_busy(resp: Any) -> None:
    assert resp.status_code == 503, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    error = resp.json()["error"]
    assert error["code"] == "root_busy", error
    assert error["retryable"] is True, error
    assert error["message"]
    assert "retry-after" not in resp.headers  # RootBusy sets no retry_after_s


def test_step_bind_while_the_root_lock_is_held_is_root_busy_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binding a run whose record has no criteria hash reads the run's checkpoints, which
    needs the runtime root lock."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "rt"
    _tenant_with_pending_import(root, "t1", "r")
    app = create_app(root=root, skills_root=tmp_path / "skills")
    key = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test").secret
    headers = {"X-API-Key": key}
    client = TestClient(app, raise_server_exceptions=False)
    created = client.post("/v1/programs", headers=headers, json={"title": "p", "steps": [_STEP]})
    assert created.status_code == 200, created.text
    pid = created.json()["program"]["program_id"]
    accepted = client.post(f"/v1/programs/{pid}/accept", headers=headers, json={"ack_disclaimer": True})
    assert accepted.status_code == 200, accepted.text
    previewed = client.post(f"/v1/programs/{pid}/steps/s1/preview", headers=headers)
    assert previewed.status_code == 200, previewed.text
    app.state.runs[("t1", "r")] = RunRecord(
        run_id="r",
        request="x",
        task_class="repo-chore",
        status="solved",
        created_at=datetime.now(timezone.utc),
        tenant_id="t1",
        terminal="solved",
        has_goal=True,
        criteria_hash=None,
    )
    held = RootLock(RuntimeLayout(root).root_lock)
    held.try_exclusive()  # what a worker-shaped import, a backup or a restore holds
    try:
        resp = client.post(
            f"/v1/programs/{pid}/steps/s1/run", headers=headers, json={"bind_run_id": "r", "workdir": "ws"}
        )
        _assert_root_busy(resp)
        assert legacy_sqlite_pending(RuntimeLayout(root).tenant("t1"))  # nothing imported
    finally:
        held.release()


_ROUTES = [
    ("create_from_pack", "post", "/v1/programs/from-pack", {"title": "p", "steps": [{}]}),
    ("create", "post", "/v1/programs", {"title": "p"}),
    ("list", "get", "/v1/programs", None),
    ("get", "get", "/v1/programs/p1", None),
    ("accept", "post", "/v1/programs/p1/accept", {"ack_disclaimer": True}),
    ("abandon", "post", "/v1/programs/p1/abandon", None),
    ("patch_step", "patch", "/v1/programs/p1/steps/s1", {}),
    ("preview_step", "post", "/v1/programs/p1/steps/s1/preview", None),
    ("run_step", "post", "/v1/programs/p1/steps/s1/run", {}),
    ("skip_step", "post", "/v1/programs/p1/steps/s1/skip", {"note": "n"}),
    ("bind_repo", "post", "/v1/programs/p1/repo-binding", {"root": "repo"}),
    ("record_tip", "post", "/v1/programs/p1/steps/s1/record-tip", {}),
    ("seed_workdir", "post", "/v1/programs/p1/steps/s1/seed-workdir", {"run_id": "r"}),
]


@pytest.mark.parametrize(("method", "verb", "path", "body"), _ROUTES, ids=[r[0] for r in _ROUTES])
def test_every_program_route_turns_root_busy_into_root_busy_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    verb: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """Every route under ``/v1/programs`` goes through the same mapper: a ``RootBusy`` from
    the service is ``root_busy``, 503, retryable, with no ``Retry-After``."""

    app = create_app(root=tmp_path / "rt", skills_root=tmp_path / "skills")
    key = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test").secret
    client = TestClient(app, raise_server_exceptions=False)
    calls: list[str] = []

    def _busy(*_args: object, **_kwargs: object) -> object:
        calls.append(method)
        raise RootBusy("cannot open the runtime root while the root lock is held")

    monkeypatch.setattr(ProgramService, method, _busy)
    kwargs: dict[str, Any] = {"headers": {"X-API-Key": key}}
    if body is not None:
        kwargs["json"] = body
    resp = client.request(verb.upper(), path, **kwargs)
    assert calls == [method], resp.text
    _assert_root_busy(resp)


def test_route_table_covers_every_program_route(tmp_path: Path) -> None:
    app = create_app(root=tmp_path / "rt", skills_root=tmp_path / "skills")
    served = {
        (verb, getattr(route, "path", ""))
        for route in app.routes
        if getattr(route, "path", "").startswith("/v1/programs")
        for verb in {m.lower() for m in getattr(route, "methods", set())}
    }
    assert served == {
        (verb, path.replace("p1", "{program_id}").replace("s1", "{step_id}")) for _, verb, path, _ in _ROUTES
    }
