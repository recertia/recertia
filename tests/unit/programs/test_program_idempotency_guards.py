"""REC-056-nits C4, C6, C7: a keyed step-run bind refuses, without binding, when the
idempotency store is missing, hands back a reservation without a lease, or finds a changed
control migration."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from recertia.persistence import idempotency as idempotency_mod
from recertia.persistence.db import MigrationChecksumError
from recertia.persistence.idempotency import Outcome, Reservation
from tests.support.idempotency import (
    TENANT,
    close_app,
    headers_for,
    key_rows,
    make_app,
    previewed_program,
    solved_run,
)


class _Api:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.app = make_app(root)
        self.client = TestClient(self.app)
        self.headers = headers_for(self.app)
        self.state = self.app.state.recertia

    def bound_program(self) -> str:
        pid, ph = previewed_program(self.client, self.headers)
        self.app.state.runs[(TENANT, "run-1")] = solved_run("run-1", ph)
        return pid

    def bind(self, pid: str) -> Any:
        body = {"bind_run_id": "run-1", "workdir": "ws", "idempotency_key": "k1"}
        return self.client.post(f"/v1/programs/{pid}/steps/s1/run", headers=self.headers, json=body)

    def step_run_and_version(self, pid: str) -> tuple[str | None, int]:
        prog = self.state.programs.get(pid, tenant_id=TENANT)
        assert prog is not None
        return prog.steps[0].current_run_id, prog.version


@pytest.fixture
def api(tmp_path: Path) -> Iterator[_Api]:
    harness = _Api(tmp_path)
    try:
        yield harness
    finally:
        close_app(harness.app)


def test_keyed_bind_without_an_idempotency_store_is_503_and_binds_nothing(api: _Api) -> None:
    pid = api.bound_program()
    before = api.step_run_and_version(pid)
    store = api.state.idempotency
    assert store is not None
    store.close()
    api.state.idempotency = None

    resp = api.bind(pid)

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "unavailable"
    assert "idempotency store" in resp.json()["error"]["message"]
    assert api.step_run_and_version(pid) == before
    assert before[0] is None


def test_reservation_without_a_lease_is_503_state_corrupt_and_binds_nothing(
    api: _Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = api.bound_program()
    before = api.step_run_and_version(pid)
    store = api.state.idempotency
    assert store is not None
    monkeypatch.setattr(store, "reserve", lambda *_a, **_k: Reservation(Outcome.RESERVED))

    resp = api.bind(pid)

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "state_corrupt"
    assert api.step_run_and_version(pid) == before
    assert before[0] is None


def test_changed_control_migration_is_503_state_corrupt_and_binds_nothing(
    api: _Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = api.bound_program()
    before = api.step_run_and_version(pid)

    def changed(*_a: Any, **_k: Any) -> list[str]:
        raise MigrationChecksumError("applied migration 003_idempotency changed on disk")

    monkeypatch.setattr(idempotency_mod, "apply_migrations", changed)

    resp = api.bind(pid)

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "state_corrupt"
    assert api.step_run_and_version(pid) == before
    assert before[0] is None
    assert not (api.root / "api-root" / "control.db").exists() or key_rows(api.root) == []
