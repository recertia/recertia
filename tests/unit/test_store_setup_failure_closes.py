"""Seven store constructors close their connection when a setup step fails.

Each constructor opens its database through ``open_db_with_init`` and runs its post-connect
steps inside ``init``. The test wraps the connection that ``sqlite3.connect`` returns for the
store's file, lets ``open_db``'s PRAGMAs through, and makes the store's first post-connect
step raise, or its last one (the schema statement, the commit, or the settings load). The
original error must propagate and ``close()`` must have run exactly once.
Counting ``close()`` directly keeps the test independent of ``ResourceWarning`` (Python 3.12
does not emit one for an unclosed sqlite3 connection).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from recertia.api.console_auth import SessionStore
from recertia.api.jobs_store import JobRunStore
from recertia.api.quotas import QuotaStore
from recertia.programs.store import ProgramStore
from recertia.proposals.store import ProposalStore
from recertia.workers.queue import RunQueue
from recertia.workspaces.registry import WorkspaceRegistry


class _SetupFailed(Exception):
    pass


class _ConnSpy:
    """A real connection that records every use, counts ``close()`` and fails one step.

    ``fail_on`` names the attribute whose assignment, or the method whose call, raises.
    """

    def __init__(self, conn: sqlite3.Connection, fail_on: str, error: BaseException) -> None:
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_fail_on", fail_on)
        object.__setattr__(self, "_error", error)
        object.__setattr__(self, "closes", 0)
        object.__setattr__(self, "uses", [])

    def close(self) -> None:
        object.__setattr__(self, "closes", self.closes + 1)
        self._conn.close()

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        self.uses.append(("execute", sql.strip()))
        if self._fail_on == "execute":
            raise self._error
        return self._conn.execute(sql, *args)

    def __setattr__(self, name: str, value: Any) -> None:
        self.uses.append(("set", name))
        if name == self._fail_on:
            raise self._error
        setattr(self._conn, name, value)

    def __getattr__(self, name: str) -> Any:
        self.uses.append(("get", name))
        if name == self._fail_on:

            def fail(*args: Any, **kwargs: Any) -> Any:
                raise self._error

            return fail
        return getattr(self._conn, name)


Case = tuple[Callable[[Path], object], str]

CASES: dict[str, Case] = {
    "SessionStore": (lambda p: SessionStore(secret="k" * 32, path=p), "executescript"),
    "JobRunStore": (JobRunStore, "row_factory"),
    "QuotaStore": (QuotaStore, "row_factory"),
    "ProgramStore": (ProgramStore, "row_factory"),
    "ProposalStore": (ProposalStore, "row_factory"),
    "RunQueue": (RunQueue, "isolation_level"),
    "WorkspaceRegistry": (WorkspaceRegistry, "row_factory"),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_first_setup_step_fails_closes_connection_once(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    construct, fail_on = CASES[name]
    path = tmp_path / f"{name}.sqlite"
    error = _SetupFailed(f"{name}: {fail_on} failed")
    real_connect = sqlite3.connect
    spies: list[_ConnSpy] = []

    def connect(database: Any, *args: Any, **kwargs: Any) -> Any:
        conn = real_connect(database, *args, **kwargs)
        if Path(database) != path:
            return conn
        spy = _ConnSpy(conn, fail_on, error)
        spies.append(spy)
        return spy

    monkeypatch.setattr(sqlite3, "connect", connect)

    with pytest.raises(_SetupFailed) as excinfo:
        construct(path)

    assert excinfo.value is error
    assert len(spies) == 1, f"{name} opened {len(spies)} connections to its file"
    spy = spies[0]
    *before, failed = spy.uses
    assert failed[1] == fail_on, spy.uses
    assert all(kind == "execute" and arg.upper().startswith("PRAGMA") for kind, arg in before), (
        f"{name}: {fail_on} is not the first post-connect step: {before}"
    )
    assert spy.closes == 1, f"{name} closed its connection {spy.closes} times"
    with pytest.raises(sqlite3.ProgrammingError):
        spy._conn.execute("SELECT 1")


class _LastStepSpy(_ConnSpy):
    """A ``_ConnSpy`` that also works as a context manager and fails only a ``CREATE`` execute.

    With ``fail_on="execute"`` the PRAGMAs of ``open_db`` still pass; the first schema
    statement raises.
    """

    def __enter__(self) -> _LastStepSpy:
        self.uses.append(("enter", ""))
        self._conn.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        self.uses.append(("exit", ""))
        return self._conn.__exit__(*exc)

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        self.uses.append(("execute", sql.strip()))
        if self._fail_on == "execute" and sql.strip().upper().startswith("CREATE"):
            raise self._error
        return self._conn.execute(sql, *args)


_SETTINGS = "load_settings"

# The step each constructor runs last after connecting. ``load_settings`` is not a
# connection method: the test patches ``recertia.settings.load_settings`` to raise.
LAST_STEPS: dict[str, str] = {
    "SessionStore": "commit",
    "JobRunStore": "execute",
    "QuotaStore": "executescript",
    "ProgramStore": "execute",
    "ProposalStore": "execute",
    "RunQueue": _SETTINGS,
    "WorkspaceRegistry": "execute",
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_last_setup_step_fails_closes_connection_once(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    construct = CASES[name][0]
    fail_on = LAST_STEPS[name]
    path = tmp_path / f"{name}.sqlite"
    error = _SetupFailed(f"{name}: {fail_on} failed")
    real_connect = sqlite3.connect
    spies: list[_LastStepSpy] = []
    settings_calls: list[int] = []

    def connect(database: Any, *args: Any, **kwargs: Any) -> Any:
        conn = real_connect(database, *args, **kwargs)
        if Path(database) != path:
            return conn
        spy = _LastStepSpy(conn, fail_on, error)
        spies.append(spy)
        return spy

    def load_settings(*args: Any, **kwargs: Any) -> Any:
        settings_calls.append(spies[0].closes if spies else -1)
        raise error

    monkeypatch.setattr(sqlite3, "connect", connect)
    if fail_on == _SETTINGS:
        import recertia.settings

        monkeypatch.setattr(recertia.settings, "load_settings", load_settings)

    with pytest.raises(_SetupFailed) as excinfo:
        construct(path)

    assert excinfo.value is error
    assert len(spies) == 1, f"{name} opened {len(spies)} connections to its file"
    spy = spies[0]
    if fail_on == _SETTINGS:
        assert settings_calls == [0], f"{name}: load_settings calls {settings_calls}"
    else:
        failed = [use for use in spy.uses if use[0] != "exit"][-1]
        assert failed[0] in {"execute", "get"} and (
            failed[1] == fail_on or (fail_on == "execute" and failed[1].upper().startswith("CREATE"))
        ), spy.uses
    assert spy.closes == 1, f"{name} closed its connection {spy.closes} times"
    with pytest.raises(sqlite3.ProgrammingError):
        spy._conn.execute("SELECT 1")
