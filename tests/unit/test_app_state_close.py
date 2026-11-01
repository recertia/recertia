"""``AppState.close`` closes every sqlite connection that ``create_app`` opened for it.

Each case takes one store's connection object before the close and checks that the
same object refuses a query afterwards, so a store that is skipped, or whose ``close``
does nothing, fails its own case. Every case also closes twice: the lifespan, test
teardown and ``close_app`` may all close the same state.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from recertia.api import create_app
from recertia.api.console_auth import SessionStore
from recertia.persistence.idempotency import KeyScope, Outcome

# Holder -> how to reach its connection from the app state.
_HOLDERS: dict[str, Callable[[Any], sqlite3.Connection]] = {
    "QuotaStore": lambda state: state.quota_store._conn,
    "WorkspaceRegistry": lambda state: state.workspace_registry._conn,
    "RunQueue": lambda state: state.queue._conn,
    "SessionStore": lambda state: state.sessions._conn,
    "ProposalStore": lambda state: state.proposals._conn,
    "ProgramStore": lambda state: state.programs._conn,
    "JobRunStore": lambda state: state.job_runs._conn,
}


def _state(tmp_path: Path) -> Any:
    return create_app(root=tmp_path / "api-root").state.recertia


def _assert_closed(conn: sqlite3.Connection, holder: str) -> None:
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
        pytest.fail(f"{holder}: the connection is still open")


@pytest.mark.parametrize("holder", sorted(_HOLDERS))
def test_close_closes_the_holders_connection(tmp_path: Path, holder: str) -> None:
    state = _state(tmp_path)
    conn = _HOLDERS[holder](state)
    assert isinstance(conn, sqlite3.Connection), holder
    conn.execute("SELECT 1")  # open before the close
    state.close()
    _assert_closed(conn, holder)
    state.close()  # a second close raises nothing
    _assert_closed(conn, holder)


def test_close_closes_an_opened_idempotency_store(tmp_path: Path) -> None:
    state = _state(tmp_path)
    store = state.idempotency
    reservation = store.reserve(KeyScope("t1", "p1", "s1", "k1"), "h1")  # a keyed bind opens it
    assert reservation.outcome is Outcome.RESERVED
    conn = store._conn
    assert isinstance(conn, sqlite3.Connection)
    state.close()
    assert store.closed and store._conn is None
    _assert_closed(conn, "IdempotencyStore")
    state.close()
    assert store.closed


def test_close_with_a_never_opened_idempotency_store(tmp_path: Path) -> None:
    state = _state(tmp_path)
    store = state.idempotency
    assert store._conn is None and not store.closed
    state.close()
    assert store.closed and store._conn is None
    state.close()
    assert store.closed


def test_close_stops_the_embedded_worker_before_closing_the_queue(tmp_path: Path) -> None:
    """The order matters: closing a store under a live worker thread can crash the interpreter.

    A reordered close (stores first, then the worker) ended the test process with a
    segmentation fault, so this spy fails on the order rather than waiting for the crash.
    """

    state = _state(tmp_path)
    state.embedded.ensure_started()
    assert state.embedded.is_running()
    conn = state.queue._conn
    order: list[str] = []
    stop, queue_close = state.embedded.stop, state.queue.close

    def spy_stop(*args: Any, **kwargs: Any) -> bool:
        order.append("embedded.stop")
        return stop(*args, **kwargs)

    def spy_queue_close() -> None:
        order.append("queue.close")
        queue_close()

    state.embedded.stop = spy_stop
    state.queue.close = spy_queue_close
    state.close()
    assert order[:2] == ["embedded.stop", "queue.close"], order
    assert not state.embedded.is_running()
    _assert_closed(conn, "RunQueue")


def test_close_leaves_every_store_open_while_the_embedded_worker_runs(tmp_path: Path) -> None:
    state = _state(tmp_path)
    conns = {holder: reach(state) for holder, reach in _HOLDERS.items()}
    real_stop = state.embedded.stop
    state.embedded.stop = lambda *args, **kwargs: False  # an item is still in flight
    state.close()
    for conn in conns.values():
        conn.execute("SELECT 1")  # still open: the worker could still use it
    state.embedded.stop = real_stop
    state.close()  # once the worker stops, a later close closes them
    for holder, conn in conns.items():
        _assert_closed(conn, holder)


def test_close_closes_the_other_stores_and_raises_the_first_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two stores' ``close`` raise: every other store is still closed and the first error wins.

    ``sessions`` is closed before ``programs``, so its error is the first one.
    """

    state = _state(tmp_path)
    reservation = state.idempotency.reserve(KeyScope("t1", "p1", "s1", "k1"), "h1")
    assert reservation.outcome is Outcome.RESERVED
    raising = {"SessionStore": "first", "ProgramStore": "second"}
    others = {holder: reach(state) for holder, reach in _HOLDERS.items() if holder not in raising}
    others["IdempotencyStore"] = state.idempotency._conn

    def raises(message: str) -> Callable[[], None]:
        def close() -> None:
            raise RuntimeError(message)

        return close

    monkeypatch.setattr(state.sessions, "close", raises("first"))
    monkeypatch.setattr(state.programs, "close", raises("second"))
    with pytest.raises(RuntimeError) as caught:
        state.close()
    assert str(caught.value) == "first", caught.value
    for holder, conn in others.items():
        _assert_closed(conn, holder)


def test_session_store_close_without_a_path_is_safe() -> None:
    store = SessionStore()
    assert store._conn is None
    store.close()
    store.close()
    assert store._conn is None


def test_session_store_close_is_safe_twice(tmp_path: Path) -> None:
    store = SessionStore(path=tmp_path / "sessions.sqlite")
    conn = store._conn
    assert conn is not None
    store.close()
    store.close()
    _assert_closed(conn, "SessionStore")
