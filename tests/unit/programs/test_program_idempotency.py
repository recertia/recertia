"""REC-056e (#135): Idempotency-Key on POST /v1/programs/{program_id}/steps/{step_id}/run.

Keys live in control.db and are written around the bind: a pending row first, then the
bind in programs.sqlite, then the row marked complete.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from recertia.api.schemas import StepRunBody
from recertia.app.programs import ProgramService, step_run_request_hash
from recertia.cli.exit import exit_code_for
from recertia.persistence.idempotency import LEASE_SECONDS, KeyScope, Outcome
from recertia.runtime.errors import IdempotencyInProgress, IdempotencyMismatch
from tests.support.idempotency import (
    STEP,
    TENANT,
    check_concurrent_same_key_different_body,
    check_concurrent_same_key_same_body,
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
        self.now = 1_000_000
        self.state.idempotency._clock = lambda: self.now

    def program(self) -> tuple[str, str]:
        return previewed_program(self.client, self.headers)

    def seed(self, run_id: str, ph: str, *, tenant_id: str = TENANT) -> None:
        self.app.state.runs[(tenant_id, run_id)] = solved_run(run_id, ph, tenant_id=tenant_id)

    def bind(self, pid: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
        url = f"/v1/programs/{pid}/steps/s1/run"
        return self.client.post(url, headers=headers or self.headers, json=body)

    def program_changes(self) -> int:
        return int(self.state.programs._conn.total_changes)

    def stored(self, pid: str, tenant_id: str = TENANT) -> Any:
        prog = self.state.programs.get(pid, tenant_id=tenant_id)
        assert prog is not None
        return prog


@pytest.fixture
def api(tmp_path: Path) -> Iterator[_Api]:
    harness = _Api(tmp_path)
    try:
        yield harness
    finally:
        close_app(harness.app)


def _body(run_id: str = "run-1", key: str = "k1", **extra: Any) -> dict[str, Any]:
    return {"bind_run_id": run_id, "workdir": "ws", "idempotency_key": key, **extra}


def _code(resp: Any) -> str:
    return str(resp.json()["error"]["code"])


def test_same_key_different_body_is_409_idempotency_mismatch_and_writes_nothing(api: _Api) -> None:
    pid, ph = api.program()
    api.seed("run-1", ph)
    api.seed("run-2", ph)
    assert api.bind(pid, _body("run-1")).status_code == 200
    rows, changes, version = key_rows(api.root), api.program_changes(), api.stored(pid).version

    resp = api.bind(pid, _body("run-2"))

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"] == {
        "code": "idempotency_mismatch",
        "message": "idempotency_key was already used with a different request body",
        "retryable": False,
    }
    assert key_rows(api.root) == rows
    assert api.program_changes() == changes
    assert api.stored(pid).version == version


def test_same_key_same_body_in_flight_is_409_idempotency_in_progress(api: _Api) -> None:
    pid, ph = api.program()
    api.seed("run-1", ph)
    body = _body()
    scope = KeyScope(TENANT, pid, "s1", "k1")
    hashed = step_run_request_hash(StepRunBody(**body))
    assert api.state.idempotency.reserve(scope, hashed).outcome is Outcome.RESERVED
    changes = api.program_changes()

    resp = api.bind(pid, body)

    assert resp.status_code == 409, resp.text
    assert (_code(resp), resp.json()["error"]["retryable"]) == ("idempotency_in_progress", True)
    assert resp.headers["Retry-After"] == "1"
    assert api.program_changes() == changes
    assert key_rows(api.root) == [(TENANT, pid, "s1", "k1", "pending", None)]


def test_a_crash_between_bind_and_complete_is_recovered_after_the_lease(
    api: _Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, ph = api.program()
    api.seed("run-1", ph)
    store = api.state.idempotency

    def crash(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("process died before complete")

    monkeypatch.setattr(store, "complete", crash)
    with pytest.raises(RuntimeError, match="process died"):
        api.bind(pid, _body())
    monkeypatch.undo()
    store._clock = lambda: api.now
    bound = api.stored(pid)
    assert bound.steps[0].current_run_id == "run-1"
    assert key_rows(api.root) == [(TENANT, pid, "s1", "k1", "pending", None)]

    early = api.bind(pid, _body())
    assert (early.status_code, _code(early)) == (409, "idempotency_in_progress")

    api.now += LEASE_SECONDS
    retry = api.bind(pid, _body())
    assert retry.status_code == 200, retry.text
    assert (retry.json()["run_id"], retry.json()["idempotent"]) == ("run-1", True)
    assert key_rows(api.root) == [(TENANT, pid, "s1", "k1", "complete", "run-1")]
    assert api.stored(pid).version == bound.version

    replay = api.bind(pid, _body())
    assert (replay.status_code, replay.json()["idempotent"]) == (200, True)


def test_a_failed_bind_deletes_its_pending_row(api: _Api) -> None:
    pid, ph = api.program()
    missing = api.bind(pid, _body("run-missing"))
    assert missing.status_code == 404, missing.text
    assert key_rows(api.root) == []

    api.seed("run-1", ph)
    applied = api.bind(pid, _body("run-1"))
    assert (applied.status_code, applied.json()["idempotent"]) == (200, False)


def test_replay_after_the_step_moved_on_is_a_conflict(api: _Api) -> None:
    pid, ph = api.program()
    api.seed("run-1", ph)
    assert api.bind(pid, _body("run-1")).status_code == 200
    prog = api.stored(pid)
    moved = prog.steps[0].model_copy(update={"current_run_id": "run-2", "run_ids": ["run-1", "run-2"]})
    api.state.programs.put(prog.model_copy(update={"steps": [moved]}))
    changes = api.program_changes()

    resp = api.bind(pid, _body("run-1"))

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "conflict"
    assert "step has moved on" in resp.json()["error"]["message"]
    assert api.program_changes() == changes
    assert api.stored(pid).steps[0].current_run_id == "run-2"


def test_the_same_key_for_another_tenant_does_not_collide(api: _Api) -> None:
    pid, ph = api.program()
    api.seed("run-1", ph)
    assert api.bind(pid, _body("run-1", "shared")).status_code == 200

    other = headers_for(api.app, "t2")
    pid2, ph2 = previewed_program(api.client, other, [STEP])
    api.seed("run-9", ph2, tenant_id="t2")
    resp = api.bind(pid2, _body("run-9", "shared"), other)

    assert resp.status_code == 200, resp.text
    assert (resp.json()["run_id"], resp.json()["idempotent"]) == ("run-9", False)
    assert key_rows(api.root) == [
        (TENANT, pid, "s1", "shared", "complete", "run-1"),
        ("t2", pid2, "s1", "shared", "complete", "run-9"),
    ]


def test_an_expired_key_is_swept_and_no_longer_honoured(api: _Api) -> None:
    from recertia.persistence.idempotency import Completion

    pid, ph = api.program()
    api.seed("run-1", ph)
    store = api.state.idempotency
    scope = KeyScope(TENANT, pid, "s1", "k1")
    # An earlier request used k1 with another body; its row is live, then expires.
    earlier = store.reserve(scope, "v1:earlier")
    assert earlier.outcome is Outcome.RESERVED and earlier.lease is not None
    assert store.complete(scope, "v1:earlier", "run-0", lease=earlier.lease) is Completion.COMPLETED
    assert _code(api.bind(pid, _body("run-1"))) == "idempotency_mismatch"

    api.now += store.ttl_seconds
    resp = api.bind(pid, _body("run-1"))

    assert resp.status_code == 200, resp.text
    assert (resp.json()["run_id"], resp.json()["idempotent"]) == ("run-1", False)
    assert api.stored(pid).steps[0].current_run_id == "run-1"
    conn = sqlite3.connect(api.root / "api-root" / "control.db")
    try:
        rows = list(conn.execute("SELECT request_hash, run_id, created_at FROM idempotency_keys"))
    finally:
        conn.close()
    assert rows == [(step_run_request_hash(StepRunBody(**_body("run-1"))), "run-1", api.now)]


@pytest.mark.parametrize("key", ["", "x" * 256, "a\nb", "caf\u00e9", "tab\there", "\x7f"])
def test_a_malformed_idempotency_key_is_422(api: _Api, key: str) -> None:
    pid, ph = api.program()
    api.seed("run-1", ph)
    resp = api.bind(pid, _body("run-1", key))
    assert resp.status_code == 422, resp.text
    assert key_rows(api.root) == []


@pytest.mark.parametrize("key", ["x" * 255, " ~", "a:b:c", "k"])
def test_a_printable_ascii_idempotency_key_is_accepted(key: str) -> None:
    assert StepRunBody(bind_run_id="r", idempotency_key=key).idempotency_key == key


def test_a_key_with_colons_does_not_collide_with_another_scope(api: _Api) -> None:
    """Main joined the scope with ':', so these two keys were one key there."""

    store = api.state.idempotency
    first = store.reserve(KeyScope(TENANT, "p:1", "s", "k"), "v1:a")
    second = store.reserve(KeyScope(TENANT, "p", "1:s", "k"), "v1:b")
    assert (first.outcome, second.outcome) == (Outcome.RESERVED, Outcome.RESERVED)


def test_program_reads_write_no_key_rows(api: _Api) -> None:
    pid, ph = api.program()
    store = api.state.idempotency
    assert store._conn is None
    for path in ("/v1/programs", f"/v1/programs/{pid}"):
        assert api.client.get(path, headers=api.headers).status_code == 200
    assert store._conn is None

    api.seed("run-1", ph)
    assert api.bind(pid, _body()).status_code == 200
    writer = store._conn
    assert writer is not None
    rows, changes = key_rows(api.root), writer.total_changes
    for path in ("/v1/programs", f"/v1/programs/{pid}"):
        assert api.client.get(path, headers=api.headers).status_code == 200
    assert (key_rows(api.root), writer.total_changes) == (rows, changes)


def test_an_unkeyed_or_plan_only_call_touches_no_key_store(api: _Api) -> None:
    pid, ph = api.program()
    plan = api.bind(pid, {"plan_only": True, "idempotency_key": "k"})
    assert plan.status_code == 200, plan.text
    api.seed("run-1", ph)
    assert api.bind(pid, {"bind_run_id": "run-1", "workdir": "ws"}).status_code == 200
    assert api.state.idempotency._conn is None


def test_app_shutdown_closes_the_idempotency_store(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    try:
        with TestClient(app) as client:
            headers = headers_for(app)
            pid, ph = previewed_program(client, headers)
            app.state.runs[(TENANT, "run-1")] = solved_run("run-1", ph)
            url = f"/v1/programs/{pid}/steps/s1/run"
            assert client.post(url, headers=headers, json=_body()).status_code == 200
            store = app.state.recertia.idempotency
            assert store._conn is not None and not store.closed
        assert store.closed and store._conn is None
        app.state.recertia.close()  # a second close is safe
        assert store.closed
    finally:
        close_app(app)


def test_request_hash_ignores_the_key_and_fills_defaults() -> None:
    bare = StepRunBody(bind_run_id="r", idempotency_key="a")
    explicit = StepRunBody(bind_run_id="r", idempotency_key="b", plan_only=False, workdir=None, budget=None)
    assert step_run_request_hash(bare) == step_run_request_hash(explicit)
    assert step_run_request_hash(bare).startswith("v1:") and len(step_run_request_hash(bare)) == 67
    assert step_run_request_hash(bare) != step_run_request_hash(StepRunBody(bind_run_id="r", workdir="w"))


def test_idempotency_errors_are_409_with_their_own_codes_and_exit_codes() -> None:
    mismatch, busy = IdempotencyMismatch("m"), IdempotencyInProgress("p", retry_after_s=1)
    assert (mismatch.http_status, mismatch.code, mismatch.retryable) == (409, "idempotency_mismatch", False)
    assert (busy.http_status, busy.code, busy.retryable) == (409, "idempotency_in_progress", True)
    assert (exit_code_for(mismatch), exit_code_for(busy)) == (5, 6)


# --- a slow (not dead) request whose row is taken over (review S1) -------------------------


def _scope_and_hash(pid: str, body: dict[str, Any]) -> tuple[KeyScope, str]:
    return KeyScope(TENANT, pid, "s1", body["idempotency_key"]), step_run_request_hash(StepRunBody(**body))


def test_a_slow_request_that_fails_after_a_takeover_leaves_the_takers_row(
    api: _Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, ph = api.program()
    api.seed("run-1", ph)
    api.seed("run-2", ph)
    body = _body("run-1")
    scope, hashed = _scope_and_hash(pid, body)
    taken: list[Any] = []

    def slow_then_fail(self: Any, **_kwargs: Any) -> Any:
        api.now += LEASE_SECONDS
        taken.append(api.state.idempotency.reserve(scope, hashed))  # a retry takes the row over
        raise RuntimeError("slow bind failed")

    monkeypatch.setattr(ProgramService, "_apply_run_step", slow_then_fail)
    with pytest.raises(RuntimeError, match="slow bind failed"):
        api.bind(pid, body)
    monkeypatch.undo()

    assert taken[0].outcome is Outcome.TAKEN_OVER
    assert key_rows(api.root) == [(TENANT, pid, "s1", "k1", "pending", None)]
    other = api.bind(pid, _body("run-2"))
    assert (other.status_code, _code(other)) == (409, "idempotency_mismatch")


def test_a_slow_request_that_lands_after_a_takeover_cannot_complete_the_takers_row(
    api: _Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, ph = api.program()
    api.seed("run-1", ph)
    body = _body("run-1")
    scope, hashed = _scope_and_hash(pid, body)
    real = ProgramService._apply_run_step

    def slow(self: Any, **kwargs: Any) -> Any:
        result = real(self, **kwargs)
        api.now += LEASE_SECONDS
        assert api.state.idempotency.reserve(scope, hashed).outcome is Outcome.TAKEN_OVER
        return result

    monkeypatch.setattr(ProgramService, "_apply_run_step", slow)
    first = api.bind(pid, body)
    monkeypatch.undo()

    assert first.status_code == 409, first.text
    assert _code(first) == "idempotency_in_progress"
    assert first.headers["Retry-After"] == "1"
    assert key_rows(api.root) == [(TENANT, pid, "s1", "k1", "pending", None)]
    bound = api.stored(pid)
    assert bound.steps[0].current_run_id == "run-1"

    api.now += LEASE_SECONDS
    retry = api.bind(pid, body)
    assert retry.status_code == 200, retry.text
    assert (retry.json()["run_id"], retry.json()["idempotent"]) == ("run-1", True)
    assert key_rows(api.root) == [(TENANT, pid, "s1", "k1", "complete", "run-1")]
    assert api.stored(pid).version == bound.version


def test_a_taker_that_completes_first_does_not_fail_the_slow_request(
    api: _Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, ph = api.program()
    api.seed("run-1", ph)
    body = _body("run-1")
    real = ProgramService._apply_run_step
    retries: list[Any] = []

    def slow(self: Any, **kwargs: Any) -> Any:
        result = real(self, **kwargs)
        api.now += LEASE_SECONDS
        retries.append(api.bind(pid, body))  # the retry takes over, sees the bind, completes
        return result

    monkeypatch.setattr(ProgramService, "_apply_run_step", slow)
    first = api.bind(pid, body)
    monkeypatch.undo()

    assert first.status_code == 200, first.text
    assert (retries[0].status_code, retries[0].json()["idempotent"]) == (200, True), retries[0].text
    assert first.json()["run_id"] == retries[0].json()["run_id"] == "run-1"
    assert key_rows(api.root) == [(TENANT, pid, "s1", "k1", "complete", "run-1")]


# --- a key row lost under a bind (review S2) -----------------------------------------------


def test_a_key_row_lost_during_the_bind_is_503_state_corrupt(
    api: _Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, ph = api.program()
    api.seed("run-1", ph)
    real = ProgramService._apply_run_step

    def lose_row(self: Any, **kwargs: Any) -> Any:
        result = real(self, **kwargs)
        conn = sqlite3.connect(api.root / "api-root" / "control.db")
        try:
            conn.execute("DELETE FROM idempotency_keys")
            conn.commit()
        finally:
            conn.close()
        return result

    monkeypatch.setattr(ProgramService, "_apply_run_step", lose_row)
    resp = api.bind(pid, _body())
    monkeypatch.undo()

    assert resp.status_code == 503, resp.text
    error = resp.json()["error"]
    assert (error["code"], error["retryable"]) == ("state_corrupt", False)
    assert error["message"] == "idempotency key row was not pending when its bind completed"
    assert key_rows(api.root) == []


def test_concurrent_same_key_same_body_binds_once(tmp_path: Path) -> None:
    check_concurrent_same_key_same_body(tmp_path)


def test_concurrent_same_key_different_body_binds_once(tmp_path: Path) -> None:
    check_concurrent_same_key_different_body(tmp_path)
