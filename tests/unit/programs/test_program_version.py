"""REC-056c: programs carry a version, and PUT needs a matching If-Match.

Every rejected PUT must leave the store untouched. The API tests count calls to
``ProgramStore.put`` and rows the store's connection changed (``total_changes``), the
same store-write counter idea the bind-check tests use.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from contracts.program import MigrationProgram, MigrationStep
from recertia.programs.store import ProgramStore
from recertia.runtime.errors import ProgramConflict
from tests.unit.test_migration_programs import _client, _issue, _step


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


def test_stale_if_match_is_program_conflict(tmp_path: Path) -> None:
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
    assert stale.status_code == 409, stale.text
    error = stale.json()["error"]
    assert error["code"] == "program_conflict"
    assert error["retryable"] is False
    assert error["details"] == {"stored_version": 2}
    assert writes.rows == 0
    assert _read(client, headers, pid) == after_first


def test_if_match_ahead_of_the_stored_version_is_program_conflict(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    prog = _create(client, headers)
    pid = prog["program_id"]

    with _count_store_writes(app) as writes:
        ahead = _put(client, headers, pid, {**prog, "title": "x"}, "7")
    assert ahead.status_code == 409, ahead.text
    assert ahead.json()["error"]["code"] == "program_conflict"
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
        with pytest.raises(ProgramConflict) as raised:
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
        with pytest.raises(ProgramConflict):
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
        with pytest.raises(ProgramConflict):
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

            def write(store: ProgramStore, n: int, version: int = expected) -> None:
                barrier.wait()
                try:
                    store.put(_program(f"w{n}"), expected_version=version)
                    result = "saved"
                except ProgramConflict:
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
