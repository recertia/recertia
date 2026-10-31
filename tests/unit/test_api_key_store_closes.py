"""ApiKeyStore closes every connection it opens (REC-109).

The spy patches ``recertia.api.auth.open_db`` with a recorder that wraps a real connection
and counts ``close()`` calls directly, so the test does not rely on ``ResourceWarning``
(Python 3.12 does not emit one for an unclosed sqlite3 connection).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any, TypeVar

import pytest

from recertia.api import auth as auth_module
from recertia.api.auth import ApiKeyStore
from recertia.persistence.db import open_db

T = TypeVar("T")


class _Boom(Exception):
    pass


class _ConnSpy:
    """A real connection that records ``close()`` and can fail one SQL statement."""

    def __init__(self, conn: sqlite3.Connection, fail_sql: str | None, error: BaseException) -> None:
        self._conn = conn
        self._fail_sql = fail_sql
        self._error = error
        self.closes = 0

    def close(self) -> None:
        self.closes += 1
        self._conn.close()

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if self._fail_sql is not None and self._fail_sql in sql:
            raise self._error
        return self._conn.execute(sql, *args)

    def __enter__(self) -> _ConnSpy:
        self._conn.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        return self._conn.__exit__(exc_type, exc, tb)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class _OpenSpy:
    def __init__(self) -> None:
        self.opened: list[_ConnSpy] = []
        self.fail_sql: str | None = None
        self.error: BaseException = _Boom("forced")

    def __call__(self, path: Path | str, **kwargs: Any) -> _ConnSpy:
        spy = _ConnSpy(open_db(path, **kwargs), self.fail_sql, self.error)
        self.opened.append(spy)
        return spy

    def run(self, op: Callable[[], T]) -> T:
        """Run ``op``; it must open at least one connection and close each exactly once."""

        start = len(self.opened)
        result = op()
        new = self.opened[start:]
        assert new, "operation opened no connection through open_db"
        assert [c.closes for c in new] == [1] * len(new)
        return result


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _OpenSpy:
    recorder = _OpenSpy()
    monkeypatch.setattr(auth_module, "open_db", recorder)
    return recorder


def test_every_api_key_store_operation_closes_its_connections(tmp_path: Path, spy: _OpenSpy) -> None:
    path = tmp_path / "keys.sqlite"
    store = spy.run(lambda: ApiKeyStore(path))
    issued = spy.run(lambda: store.issue(tenant_id="alpha", scopes={"runs"}, actor="op"))
    # Unstructured key: the early-rejection audit block.
    assert spy.run(lambda: store.authenticate("not-a-structured-key")) is None
    # Structured key, found.
    principal = spy.run(lambda: store.authenticate(issued.secret))
    assert principal is not None and principal.tenant_id == "alpha"
    # Structured key, not found.
    assert spy.run(lambda: store.authenticate("rec_key_0000000000000000.missing")) is None
    keys = spy.run(store.list_keys)
    assert [k["key_id"] for k in keys] == [issued.key_id]
    # Live key: revoke returns True from inside the block.
    assert spy.run(lambda: store.revoke(issued.key_id, actor="op")) is True
    # Unknown key: revoke falls through to False.
    assert spy.run(lambda: store.revoke("key_ffffffffffffffff", actor="op")) is False
    assert sum(c.closes for c in spy.opened) == len(spy.opened)


def test_error_inside_the_block_still_closes_and_propagates(tmp_path: Path, spy: _OpenSpy) -> None:
    store = ApiKeyStore(tmp_path / "keys.sqlite")
    spy.fail_sql = "INSERT INTO api_keys"
    start = len(spy.opened)
    with pytest.raises(_Boom) as excinfo:
        store.issue(tenant_id="alpha", scopes={"runs"}, actor="op")
    assert excinfo.value is spy.error
    new = spy.opened[start:]
    assert len(new) == 1 and new[0].closes == 1
    spy.fail_sql = None
    assert store.list_keys() == []


def test_issue_and_revoke_are_committed_for_a_second_store(tmp_path: Path) -> None:
    path = tmp_path / "keys.sqlite"
    first = ApiKeyStore(path)
    issued = first.issue(tenant_id="alpha", scopes={"runs"}, actor="op")
    second = ApiKeyStore(path)
    principal = second.authenticate(issued.secret)
    assert principal is not None and principal.tenant_id == "alpha"
    assert first.revoke(issued.key_id, actor="op") is True
    assert ApiKeyStore(path).authenticate(issued.secret) is None
    assert second.authenticate(issued.secret) is None
