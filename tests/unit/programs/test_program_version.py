"""REC-056c: programs carry a version, and PUT needs a matching If-Match.

Every rejected PUT must leave the store untouched. The API tests count calls to
``ProgramStore.put`` and rows the store's connection changed (``total_changes``), the
same store-write counter idea the bind-check tests use.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from contracts.goal import Goal, compile_goal
from contracts.program import MigrationProgram, MigrationStep
from recertia.programs.materialize import preview_hash
from recertia.programs.store import ProgramStore
from recertia.runtime.errors import PreconditionFailed
from tests.support.idempotency import (
    check_restart_survives,
    close_app,
    headers_for,
    key_rows,
    make_app,
    solved_run,
)
from tests.unit.test_migration_programs import _client, _init_git_repo, _issue, _step


class _StoreWrites:
    def __init__(self) -> None:
        self.calls = 0
        self.rows = 0


@contextmanager
def _count_store_writes(app: Any) -> Iterator[_StoreWrites]:
    """Count ``put`` calls and the rows they changed while the block runs."""

    store: ProgramStore = app.state.recertia.programs
    counter = _StoreWrites()
    real_put = store.put

    def counting_put(*args: Any, **kwargs: Any) -> MigrationProgram:
        counter.calls += 1
        return real_put(*args, **kwargs)

    before = store._conn.total_changes
    store.put = counting_put  # type: ignore[method-assign]
    try:
        yield counter
    finally:
        del store.put
        counter.rows = store._conn.total_changes - before


def _create(client: TestClient, headers: dict[str, str], title: str = "pack") -> dict[str, Any]:
    created = client.post(
        "/v1/programs", headers=headers, json={"title": title, "steps": [_step("s1", 0)]}
    )
    assert created.status_code == 200, created.text
    return dict(created.json()["program"])


def _read(client: TestClient, headers: dict[str, str], pid: str) -> dict[str, Any]:
    got = client.get(f"/v1/programs/{pid}", headers=headers)
    assert got.status_code == 200, got.text
    return dict(got.json()["program"])


def _put(
    client: TestClient,
    headers: dict[str, str],
    pid: str,
    body: dict[str, Any],
    if_match: str | None,
) -> Any:
    extra = {} if if_match is None else {"If-Match": if_match}
    return client.put(f"/v1/programs/{pid}", headers={**headers, **extra}, json=body)


def test_stale_if_match_is_precondition_failed(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    prog = _create(client, headers)
    pid = prog["program_id"]
    assert prog["version"] == 1

    first = _put(client, headers, pid, {**prog, "title": "first writer"}, "1")
    assert first.status_code == 200, first.text
    assert first.json()["program"]["version"] == 2
    after_first = _read(client, headers, pid)

    with _count_store_writes(app) as writes:
        stale = _put(client, headers, pid, {**prog, "title": "second writer"}, "1")
    assert stale.status_code == 412, stale.text
    error = stale.json()["error"]
    assert error["code"] == "precondition_failed"
    assert error["retryable"] is False
    assert error["details"] == {"stored_version": 2}
    assert writes.rows == 0
    assert _read(client, headers, pid) == after_first


def test_if_match_ahead_of_the_stored_version_is_precondition_failed(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    prog = _create(client, headers)
    pid = prog["program_id"]

    with _count_store_writes(app) as writes:
        ahead = _put(client, headers, pid, {**prog, "title": "x"}, "7")
    assert ahead.status_code == 412, ahead.text
    assert ahead.json()["error"]["code"] == "precondition_failed"
    assert writes.rows == 0
    assert _read(client, headers, pid) == prog


def test_matching_if_match_replaces_the_program(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    pid = _create(client, headers)["program_id"]
    read = _read(client, headers, pid)
    assert _read(client, headers, pid)["version"] == read["version"] == 1  # reads do not bump

    body = {**read, "title": "renamed", "intent": "new intent", "status": "active"}
    with _count_store_writes(app) as writes:
        resp = _put(client, headers, pid, body, f'"{read["version"]}"')
    assert resp.status_code == 200, resp.text
    saved = resp.json()["program"]
    assert writes.calls == 1
    assert saved["version"] == 2
    assert saved["title"] == "renamed"
    assert saved["intent"] == "new intent"
    assert saved["status"] == "draft"  # server-owned: a PUT cannot accept a program
    assert saved["created_at"] == read["created_at"]
    assert resp.headers["ETag"] == '"2"'
    assert _read(client, headers, pid) == saved

    again = _put(client, headers, pid, {**saved, "title": "third"}, "2")
    assert again.status_code == 200, again.text
    assert again.json()["program"]["version"] == 3


@pytest.mark.parametrize("if_match", [None, "", "  "])
def test_missing_if_match_is_precondition_required(tmp_path: Path, if_match: str | None) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    prog = _create(client, headers)
    pid = prog["program_id"]

    with _count_store_writes(app) as writes:
        resp = _put(client, headers, pid, {**prog, "title": "blind"}, if_match)
    assert resp.status_code == 428, resp.text
    error = resp.json()["error"]
    assert error["code"] == "precondition_required"
    assert error["retryable"] is False
    assert (writes.calls, writes.rows) == (0, 0)
    assert _read(client, headers, pid) == prog


@pytest.mark.parametrize("if_match", ["*", 'W/"1"', "abc", "1, 2", '"1', "-1"])
def test_malformed_if_match_is_bad_request(tmp_path: Path, if_match: str) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    prog = _create(client, headers)
    pid = prog["program_id"]

    with _count_store_writes(app) as writes:
        resp = _put(client, headers, pid, {**prog, "title": "x"}, if_match)
    assert resp.status_code == 400, resp.text
    assert (writes.calls, writes.rows) == (0, 0)
    assert _read(client, headers, pid) == prog


def test_put_of_an_unknown_program_is_not_found(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    with _count_store_writes(app) as writes:
        resp = _put(client, headers, "nope", {"title": "x"}, "1")
    assert resp.status_code == 404, resp.text
    assert (writes.calls, writes.rows) == (0, 0)


def test_put_of_an_accepted_program_is_conflict(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    pid = _create(client, headers)["program_id"]
    accepted = client.post(f"/v1/programs/{pid}/accept", headers=headers, json={"ack_disclaimer": True})
    assert accepted.status_code == 200, accepted.text
    current = _read(client, headers, pid)

    with _count_store_writes(app) as writes:
        resp = _put(client, headers, pid, {**current, "title": "x"}, str(current["version"]))
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "conflict"
    assert (writes.calls, writes.rows) == (0, 0)


def test_put_of_an_accepted_program_with_a_stale_if_match_is_precondition_failed(tmp_path: Path) -> None:
    """A stale If-Match is checked before the status: 412 with the stored version, not 409."""

    client, app = _client(tmp_path)
    headers = _issue(app)
    pid = _create(client, headers)["program_id"]
    accepted = client.post(f"/v1/programs/{pid}/accept", headers=headers, json={"ack_disclaimer": True})
    assert accepted.status_code == 200, accepted.text
    current = _read(client, headers, pid)
    assert current["version"] == 2

    with _count_store_writes(app) as writes:
        resp = _put(client, headers, pid, {**current, "title": "x"}, "1")
    assert resp.status_code == 412, resp.text
    error = resp.json()["error"]
    assert error["code"] == "precondition_failed"
    assert error["details"]["stored_version"] == 2
    assert (writes.calls, writes.rows) == (0, 0)


def test_put_of_another_tenants_program_is_not_found(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    prog = _create(client, headers)
    other = _issue(app, tenant_id="t2")
    with _count_store_writes(app) as writes:
        resp = _put(client, other, prog["program_id"], {**prog, "title": "x"}, "1")
    assert resp.status_code == 404, resp.text
    assert (writes.calls, writes.rows) == (0, 0)
    assert _read(client, headers, prog["program_id"]) == prog


def test_put_of_an_invalid_program_is_bad_request(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    prog = _create(client, headers)
    pid = prog["program_id"]
    body = {**prog, "steps": [_step("s1", 0), _step("s2", 0)]}  # duplicate ordinal

    with _count_store_writes(app) as writes:
        resp = _put(client, headers, pid, body, "1")
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "bad_request"
    assert (writes.calls, writes.rows) == (0, 0)
    assert _read(client, headers, pid) == prog


def test_etag_is_the_quoted_version_and_round_trips_as_if_match(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    pid = _create(client, headers)["program_id"]

    got = client.get(f"/v1/programs/{pid}", headers=headers)
    assert got.status_code == 200, got.text
    assert got.headers["ETag"] == '"1"'
    body = {**got.json()["program"], "title": "renamed"}

    put = _put(client, headers, pid, body, got.headers["ETag"])
    assert put.status_code == 200, put.text
    assert put.headers["ETag"] == '"2"' == f'"{put.json()["program"]["version"]}"'

    again = client.get(f"/v1/programs/{pid}", headers=headers)
    assert again.headers["ETag"] == put.headers["ETag"]
    stale = _put(client, headers, pid, body, got.headers["ETag"])
    assert stale.status_code == 412, stale.text
    assert "etag" not in {k.lower() for k in stale.headers}


@contextmanager
def _write_after_read(app: Any, change: dict[str, Any]) -> Iterator[None]:
    """Commit ``change`` straight to the store right after the next ``get`` reads the program.

    This is a concurrent writer landing between a service's read and its write.
    """

    store: ProgramStore = app.state.recertia.programs
    real_get = store.get
    fired = False

    def get_then_write(program_id: str, *, tenant_id: str) -> MigrationProgram | None:
        nonlocal fired
        snapshot = real_get(program_id, tenant_id=tenant_id)
        if snapshot is not None and not fired:
            fired = True
            store.put(snapshot.model_copy(update=change))
        return snapshot

    store.get = get_then_write  # type: ignore[method-assign]
    try:
        yield
    finally:
        del store.get


@pytest.mark.parametrize("if_match", ["1", "2"])
def test_accept_between_read_and_write_is_precondition_failed(
    tmp_path: Path, if_match: str
) -> None:
    """A PUT must not save fields from a read that a concurrent accept has made stale.

    "1" is the version the PUT read; "2" is the version after the accept, which a
    client can guess by adding 1. Neither may revert the accepted program to draft.
    """

    client, app = _client(tmp_path)
    headers = _issue(app)
    prog = _create(client, headers)
    pid = prog["program_id"]
    acked = "2026-10-30T00:00:00+00:00"

    with _write_after_read(app, {"status": "active", "disclaimer_acked_at": acked}):
        resp = _put(client, headers, pid, {**prog, "title": "late"}, if_match)
    assert resp.status_code == 412, resp.text
    assert resp.json()["error"]["code"] == "precondition_failed"
    stored = app.state.recertia.programs.get(pid, tenant_id="t1")
    assert (stored.status, stored.disclaimer_acked_at, stored.version) == ("active", acked, 2)
    assert stored.title == prog["title"]


def _accept(client: TestClient, headers: dict[str, str], pid: str) -> None:
    accepted = client.post(f"/v1/programs/{pid}/accept", headers=headers, json={"ack_disclaimer": True})
    assert accepted.status_code == 200, accepted.text


def test_get_saves_a_real_refresh_with_version_plus_one(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    pid = _create(client, headers)["program_id"]
    _accept(client, headers, pid)
    store: ProgramStore = app.state.recertia.programs
    accepted = store.get(pid, tenant_id="t1")
    assert accepted is not None and accepted.steps[0].status == "ready"
    # A stored step left planned with no predecessor: the refresh makes it ready.
    planned = accepted.model_copy(
        update={"steps": [accepted.steps[0].model_copy(update={"status": "planned"})]}
    )
    before = store.put(planned).version

    with _count_store_writes(app) as writes:
        got = _read(client, headers, pid)
    assert got["steps"][0]["status"] == "ready"
    assert got["version"] == before + 1
    assert writes.calls == 1
    stored = store.get(pid, tenant_id="t1")
    assert stored is not None
    assert (stored.steps[0].status, stored.version) == ("ready", before + 1)

    with _count_store_writes(app) as writes:
        assert _read(client, headers, pid)["version"] == before + 1
    assert (writes.calls, writes.rows) == (0, 0)


def test_get_refresh_does_not_overwrite_a_write_that_landed_after_its_read(
    tmp_path: Path,
) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    pid = _create(client, headers)["program_id"]
    _accept(client, headers, pid)
    store: ProgramStore = app.state.recertia.programs
    accepted = store.get(pid, tenant_id="t1")
    assert accepted is not None
    planned = accepted.model_copy(
        update={"steps": [accepted.steps[0].model_copy(update={"status": "planned"})]}
    )
    before = store.put(planned).version

    with _write_after_read(app, {"title": "concurrent"}):
        got = _read(client, headers, pid)
    stored = store.get(pid, tenant_id="t1")
    assert stored is not None
    assert (stored.title, stored.version) == ("concurrent", before + 1)
    assert (got["title"], got["version"]) == ("concurrent", before + 1)


def test_preview_saves_and_bumps_the_version_only_when_its_hash_changes(
    tmp_path: Path,
) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    pid = _create(client, headers)["program_id"]
    url = f"/v1/programs/{pid}/steps/s1/preview"

    first = client.post(url, headers=headers)
    assert first.status_code == 200, first.text
    after_first = _read(client, headers, pid)
    assert after_first["version"] == 2
    assert after_first["steps"][0]["criteria_preview_hash"] == first.json()["criteria_preview_hash"]

    with _count_store_writes(app) as writes:
        second = client.post(url, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["criteria_preview_hash"] == first.json()["criteria_preview_hash"]
    assert (writes.calls, writes.rows) == (0, 0)
    assert _read(client, headers, pid) == after_first

    put = _put(client, headers, pid, {**after_first, "title": "edited"}, "2")
    assert put.status_code == 200, put.text


# --- REC-094: intent routes pin their save to the version they read -------------------


class _RaceApi:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.app = make_app(root)
        self.client = TestClient(self.app)
        self.headers = headers_for(self.app)
        self.store: ProgramStore = self.app.state.recertia.programs

    def post(self, url: str, body: dict[str, Any] | None = None) -> Any:
        return self.client.post(url, headers=self.headers, json=body)

    def stored(self, pid: str) -> MigrationProgram:
        prog = self.store.get(pid, tenant_id="t1")
        assert prog is not None
        return prog


@pytest.fixture
def race_api(tmp_path: Path) -> Iterator[_RaceApi]:
    harness = _RaceApi(tmp_path)
    try:
        yield harness
    finally:
        close_app(harness.app)


class _Race:
    def __init__(self) -> None:
        self.route_puts = 0
        self.competing: MigrationProgram | None = None


@contextmanager
def _write_before_save(
    app: Any, change: Callable[[MigrationProgram], MigrationProgram]
) -> Iterator[_Race]:
    """Commit ``change(stored)`` to the store just before the route's first save.

    The route has already read the program, so this is a concurrent writer landing between
    the route's read and its save. ``route_puts`` counts the route's own ``put`` calls.
    """

    store: ProgramStore = app.state.recertia.programs
    real_put = store.put
    race = _Race()

    def competing_then_put(program: MigrationProgram, **kwargs: Any) -> MigrationProgram:
        race.route_puts += 1
        if race.competing is None:
            stored = store.get(program.program_id, tenant_id=program.tenant_id)
            assert stored is not None
            race.competing = real_put(change(stored))
        return real_put(program, **kwargs)

    store.put = competing_then_put  # type: ignore[method-assign]
    try:
        yield race
    finally:
        del store.put


@contextmanager
def _write_after_first_read(
    app: Any, change: Callable[[MigrationProgram], MigrationProgram]
) -> Iterator[_Race]:
    """Commit ``change(stored)`` to the store right after the route's first ``get`` returns.

    Unlike ``_write_before_save``, the competing write lands before anything else the route
    does, so a route that read the version again just before saving would see the new
    version and save over the write. ``route_puts`` counts the route's own ``put`` calls.
    """

    store: ProgramStore = app.state.recertia.programs
    real_get, real_put = store.get, store.put
    race = _Race()

    def get_then_competing(program_id: str, **kwargs: Any) -> MigrationProgram | None:
        read = real_get(program_id, **kwargs)
        if race.competing is None and read is not None:
            race.competing = real_put(change(read))
        return read

    def counting_put(program: MigrationProgram, **kwargs: Any) -> MigrationProgram:
        race.route_puts += 1
        return real_put(program, **kwargs)

    store.get = get_then_competing  # type: ignore[method-assign]
    store.put = counting_put  # type: ignore[method-assign]
    try:
        yield race
    finally:
        del store.get, store.put


def _retitle(stored: MigrationProgram) -> MigrationProgram:
    return stored.model_copy(update={"title": "concurrent"})


def _ok(resp: Any) -> Any:
    assert resp.status_code == 200, resp.text
    return resp


def _new_program(api: _RaceApi, steps: list[dict[str, Any]] | None = None) -> str:
    created = _ok(api.post("/v1/programs", {"title": "pack", "steps": steps or [_step("s1", 0)]}))
    return str(created.json()["program"]["program_id"])


def _bind_repo(api: _RaceApi, pid: str) -> None:
    repo = api.root / "api-root" / "repo_bindings" / "t1" / "app"
    if not repo.exists():
        _init_git_repo(repo)
    _ok(api.post(f"/v1/programs/{pid}/repo-binding", {"root": "app"}))


INTENT_ROUTES = [
    "accept",
    "abandon",
    "patch_step",
    "run_step_plan_only",
    "run_step_unbound",
    "run_step_bind",
    "run_step_bind_keyed",
    "skip_step",
    "bind_repo",
    "record_tip",
]


def _setup_route(api: _RaceApi, route: str) -> tuple[str, str, str, dict[str, Any]]:
    """Prepare a program so ``route`` reaches its save: ``(program_id, method, url, json)``."""

    steps = [_step("s1", 0), _step("s2", 1)] if route == "skip_step" else None
    pid = _new_program(api, steps)
    base = f"/v1/programs/{pid}"
    if route in {"bind_repo", "record_tip"}:
        _bind_repo(api, pid)
    if route not in {"accept", "abandon", "patch_step", "bind_repo"}:
        _accept(api.client, api.headers, pid)
    if route in {"run_step_bind", "run_step_bind_keyed"}:
        ph = _ok(api.post(f"{base}/steps/s1/preview")).json()["criteria_preview_hash"]
        api.app.state.runs[("t1", "run-1")] = solved_run("run-1", ph)
    bind = {"bind_run_id": "run-1", "workdir": "ws"}
    requests: dict[str, tuple[str, str, dict[str, Any]]] = {
        "accept": ("POST", f"{base}/accept", {"ack_disclaimer": True}),
        "abandon": ("POST", f"{base}/abandon", {}),
        "patch_step": ("PATCH", f"{base}/steps/s1", {"title": "patched"}),
        "run_step_plan_only": ("POST", f"{base}/steps/s1/run", {"plan_only": True}),
        "run_step_unbound": ("POST", f"{base}/steps/s1/run", {"workdir": "ws"}),
        "run_step_bind": ("POST", f"{base}/steps/s1/run", bind),
        "run_step_bind_keyed": ("POST", f"{base}/steps/s1/run", {**bind, "idempotency_key": "k1"}),
        "skip_step": ("POST", f"{base}/steps/s1/skip", {"note": "done offline"}),
        "bind_repo": ("POST", f"{base}/repo-binding", {"root": "app"}),
        "record_tip": ("POST", f"{base}/steps/s1/record-tip", {"use_binding_root": True}),
    }
    method, url, body = requests[route]
    return pid, method, url, body


@pytest.mark.parametrize("route", INTENT_ROUTES)
def test_intent_route_saves_once_when_no_write_races_it(race_api: _RaceApi, route: str) -> None:
    """Control for the race test below: with no competing write the route saves, once."""

    pid, method, url, body = _setup_route(race_api, route)
    before = race_api.stored(pid).version
    with _count_store_writes(race_api.app) as writes:
        resp = race_api.client.request(method, url, headers=race_api.headers, json=body)
    assert resp.status_code == 200, resp.text
    assert writes.calls == 1
    assert race_api.stored(pid).version == before + 1


@pytest.mark.parametrize("route", INTENT_ROUTES)
def test_intent_route_after_a_concurrent_write_does_not_overwrite_it(race_api: _RaceApi, route: str) -> None:
    """A write that lands between the route's read and its save wins; the route gets 409.

    The 409 is ``conflict`` with the version now stored, the route calls ``put`` once (no
    retry), and the stored program is exactly the competing write.
    """

    pid, method, url, body = _setup_route(race_api, route)
    with _write_before_save(race_api.app, _retitle) as race:
        resp = race_api.client.request(method, url, headers=race_api.headers, json=body)

    assert race.competing is not None
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"] == {
        "code": "conflict",
        "message": "program changed while this request ran; re-read it and retry",
        "retryable": False,
        "details": {"stored_version": race.competing.version},
    }
    assert "etag" not in {k.lower() for k in resp.headers}
    assert race.route_puts == 1
    assert race_api.stored(pid) == race.competing
    if route == "run_step_bind_keyed":
        assert key_rows(race_api.root) == []


# The keyed bind reads the program once before reserving its key and again under the
# reservation (so a replay sees the bind it recorded); its save pins to that second read.
FIRST_READ_ROUTES = [route for route in INTENT_ROUTES if route != "run_step_bind_keyed"]


@pytest.mark.parametrize("route", FIRST_READ_ROUTES)
def test_intent_route_saves_the_version_it_first_read_not_a_later_one(
    race_api: _RaceApi, route: str
) -> None:
    """A write that lands right after the route's first read still wins; the route gets 409.

    So the route pins its save to the version from that first read, not to one read later.
    """

    pid, method, url, body = _setup_route(race_api, route)
    with _write_after_first_read(race_api.app, _retitle) as race:
        resp = race_api.client.request(method, url, headers=race_api.headers, json=body)

    assert race.competing is not None
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "conflict"
    assert resp.json()["error"]["details"] == {"stored_version": race.competing.version}
    assert race.route_puts == 1
    assert race_api.stored(pid) == race.competing


def test_keyed_bind_that_loses_the_race_leaves_no_pending_key_row(race_api: _RaceApi) -> None:
    """The 409 deletes the bind's pending row, so a retry with the same key binds."""

    pid, _, url, body = _setup_route(race_api, "run_step_bind_keyed")
    with _write_before_save(race_api.app, _retitle):
        lost = race_api.post(url, body)
    assert lost.status_code == 409, lost.text
    assert lost.json()["error"]["code"] == "conflict"
    assert key_rows(race_api.root) == []

    retried = _ok(race_api.post(url, body)).json()
    assert (retried["idempotent"], retried["run_id"]) == (False, "run-1")
    assert retried["program"]["title"] == "concurrent"
    assert [row[4:] for row in key_rows(race_api.root)] == [("complete", "run-1")]
    assert race_api.stored(pid).steps[0].current_run_id == "run-1"


def test_preview_after_a_concurrent_write_rereads_instead_of_overwriting(race_api: _RaceApi) -> None:
    """The preview's hash save loses: 200, nothing saved over the write, the stored program read back."""

    pid = _new_program(race_api)
    url = f"/v1/programs/{pid}/steps/s1/preview"

    def new_goal(stored: MigrationProgram) -> MigrationProgram:
        step = stored.steps[0]
        desired = [d.model_copy(update={"path": "OTHER.md"}) for d in step.goal.desired]
        goal = step.goal.model_copy(update={"desired": desired})
        changed = step.model_copy(update={"goal": goal, "goal_revision": step.goal_revision + 1})
        return stored.model_copy(update={"steps": [changed]})

    with _write_before_save(race_api.app, new_goal) as race:
        resp = race_api.post(url)

    assert resp.status_code == 200, resp.text
    assert race.route_puts == 1
    stored = race_api.stored(pid)
    assert stored == race.competing
    assert stored.steps[0].criteria_preview_hash is None
    preview = resp.json()
    goal = Goal.model_validate(preview["goal"])
    assert [d.path for d in goal.desired] == ["OTHER.md"]
    assert preview["criteria_preview_hash"] == preview_hash(goal)
    assert preview["criteria"] == [c.model_dump(mode="json") for c in compile_goal(goal)]
    assert preview["freeze_enforcement"] == stored.freeze_enforcement

    # With no race the next preview saves the hash of the stored goal.
    again = _ok(race_api.post(url)).json()
    assert again["criteria_preview_hash"] == preview["criteria_preview_hash"]
    saved = race_api.stored(pid)
    assert saved.steps[0].criteria_preview_hash == preview["criteria_preview_hash"]
    assert saved.version == stored.version + 1


def _failing_seed(api: _RaceApi) -> tuple[str, str]:
    pid = _new_program(api)
    _bind_repo(api, pid)
    _accept(api.client, api.headers, pid)
    return pid, f"/v1/programs/{pid}/steps/s1/seed-workdir"


_BAD_TIP = {"run_id": "r1", "tip_sha": "deadbeef" * 5}


def _assert_original_seed_error(resp: Any) -> str:
    """The checkout's ``ConfigError``, which program routes send as 400 ``bad_request``."""

    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert (error["code"], error["retryable"]) == ("bad_request", False)
    assert "details" not in error
    assert "deadbeef" in error["message"]
    return str(error["message"])


def test_seed_workdir_with_a_failing_checkout_is_still_bad_request(race_api: _RaceApi) -> None:
    pid, url = _failing_seed(race_api)
    before = race_api.stored(pid).version
    with _count_store_writes(race_api.app) as writes:
        resp = race_api.post(url, _BAD_TIP)
    _assert_original_seed_error(resp)
    assert writes.calls == 1
    stored = race_api.stored(pid)
    assert (stored.status, stored.steps[0].status, stored.version) == ("blocked", "failed", before + 1)


def test_seed_workdir_failure_mark_that_loses_the_race_keeps_the_original_error(race_api: _RaceApi) -> None:
    """The original checkout error wins, the competing write stands, the mark is skipped."""

    pid, url = _failing_seed(race_api)
    with _write_before_save(race_api.app, _retitle) as race:
        lost = race_api.post(url, _BAD_TIP)
    message = _assert_original_seed_error(lost)
    assert race.route_puts == 1
    stored = race_api.stored(pid)
    assert stored == race.competing
    assert (stored.status, stored.steps[0].status) == ("active", "ready")

    # The same error as with no race.
    other, other_url = _failing_seed(race_api)
    assert _assert_original_seed_error(race_api.post(other_url, _BAD_TIP)) == message
    assert race_api.stored(other).status == "blocked"


# --- ProgramStore -----------------------------------------------------------------------


def _program(title: str = "pack") -> MigrationProgram:
    return MigrationProgram(
        program_id="p1",
        tenant_id="t1",
        title=title,
        steps=[MigrationStep.model_validate(_step("s1", 0))],
    )


def test_store_stale_if_match_raises(tmp_path: Path) -> None:
    store = ProgramStore(tmp_path / "programs.sqlite")
    try:
        assert store.put(_program()).version == 1
        assert store.put(_program("v2"), expected_version=1).version == 2
        before = store._conn.total_changes
        with pytest.raises(PreconditionFailed) as raised:
            store.put(_program("stale"), expected_version=1)
        assert raised.value.details == {"stored_version": 2}
        assert store._conn.total_changes == before
        stored = store.get("p1", tenant_id="t1")
        assert stored is not None
        assert (stored.title, stored.version) == ("v2", 2)
    finally:
        store.close()


def test_store_save_without_expected_version_still_bumps_it(tmp_path: Path) -> None:
    store = ProgramStore(tmp_path / "programs.sqlite")
    try:
        assert [store.put(_program(f"t{i}")).version for i in range(3)] == [1, 2, 3]
        stored = store.get("p1", tenant_id="t1")
        assert stored is not None and stored.version == 3
        assert store.list(tenant_id="t1")[0].version == 3
    finally:
        store.close()


def test_store_ignores_the_callers_version(tmp_path: Path) -> None:
    store = ProgramStore(tmp_path / "programs.sqlite")
    try:
        assert store.put(_program().model_copy(update={"version": 9})).version == 1
        assert store.put(_program().model_copy(update={"version": 9})).version == 2
    finally:
        store.close()


def test_store_expected_version_for_a_missing_program_raises(tmp_path: Path) -> None:
    store = ProgramStore(tmp_path / "programs.sqlite")
    try:
        with pytest.raises(PreconditionFailed):
            store.put(_program(), expected_version=1)
        assert store.get("p1", tenant_id="t1") is None
    finally:
        store.close()


def test_store_row_saved_before_versions_reads_as_version_1(tmp_path: Path) -> None:
    store = ProgramStore(tmp_path / "programs.sqlite")
    try:
        legacy = _program("legacy").model_dump(mode="json")
        del legacy["version"]
        with store._conn:
            store._conn.execute(
                "INSERT INTO programs(program_id, tenant_id, status, payload, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("p1", "t1", "draft", json.dumps(legacy), "2026-01-01T00:00:00+00:00"),
            )
        stored = store.get("p1", tenant_id="t1")
        assert stored is not None and stored.version == 1
        with pytest.raises(PreconditionFailed):
            store.put(_program("stale"), expected_version=2)
        assert store.put(_program("new"), expected_version=1).version == 2
    finally:
        store.close()


def test_store_concurrent_writers_of_one_version_save_once(tmp_path: Path) -> None:
    """Writers on separate connections race the same version: exactly one saves."""

    path = tmp_path / "programs.sqlite"
    seed = ProgramStore(path)
    seed.put(_program())
    writers = 8
    stores = [ProgramStore(path) for _ in range(writers)]
    try:
        for expected in range(1, 6):
            barrier = threading.Barrier(writers)
            outcomes: list[str] = []
            guard = threading.Lock()

            def write(
                store: ProgramStore,
                n: int,
                version: int = expected,
                barrier: threading.Barrier = barrier,
                guard: threading.Lock = guard,
                outcomes: list[str] = outcomes,
            ) -> None:
                barrier.wait()
                try:
                    store.put(_program(f"w{n}"), expected_version=version)
                    result = "saved"
                except PreconditionFailed:
                    result = "conflict"
                with guard:
                    outcomes.append(result)

            threads = [
                threading.Thread(target=write, args=(store, n)) for n, store in enumerate(stores)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            assert sorted(outcomes) == ["conflict"] * (writers - 1) + ["saved"]
            stored = seed.get("p1", tenant_id="t1")
            assert stored is not None and stored.version == expected + 1
    finally:
        for store in stores:
            store.close()
        seed.close()


def test_a_428_http_exception_under_v1_is_precondition_required() -> None:
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from starlette.requests import Request

    from recertia.api.errors import envelope_http_exception

    request = Request({"type": "http", "method": "PUT", "path": "/v1/programs/p1", "headers": []})
    resp = envelope_http_exception(request, StarletteHTTPException(428, "If-Match is required"))
    assert resp.status_code == 428
    assert json.loads(bytes(resp.body)) == {
        "error": {
            "code": "precondition_required",
            "message": "If-Match is required",
            "retryable": False,
        }
    }


def test_a_412_http_exception_under_v1_is_precondition_failed() -> None:
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from starlette.requests import Request

    from recertia.api.errors import envelope_http_exception

    request = Request({"type": "http", "method": "PUT", "path": "/v1/programs/p1", "headers": []})
    resp = envelope_http_exception(request, StarletteHTTPException(412, "stale version"))
    assert resp.status_code == 412
    assert json.loads(bytes(resp.body))["error"]["code"] == "precondition_failed"


# --- Idempotency-Key across a restart (REC-056e, #135) ----------------------------------


def test_idempotency_survives_restart(tmp_path: Path) -> None:
    """A step run recorded under an Idempotency-Key replays its run_id after a restart.

    The second app is built from the same root, so only what was written to disk is left:
    a key kept in process memory is forgotten and the replay is not answered as idempotent.
    """

    check_restart_survives(tmp_path)
