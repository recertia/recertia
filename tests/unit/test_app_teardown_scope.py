"""An app built by a module- or class-scoped fixture stays open for every test in its scope.

Test teardown closes the app states that a test builds. A state built while a wider-scoped
fixture is being set up belongs to that fixture: it is closed after the fixture's own
teardown, not after the first test that requested it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from recertia.api import create_app


def _connections(app: Any) -> dict[str, sqlite3.Connection]:
    state = app.state.recertia
    return {
        "RunQueue": state.queue._conn,
        "SessionStore": state.sessions._conn,
        "ProgramStore": state.programs._conn,
    }


def _assert_open(app: Any) -> None:
    for holder, conn in _connections(app).items():
        try:
            conn.execute("SELECT 1")
        except sqlite3.ProgrammingError as exc:
            pytest.fail(f"{holder}: the scoped app was closed under a later test: {exc}")


@pytest.fixture(scope="module")
def module_app(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    app = create_app(root=tmp_path_factory.mktemp("module-app") / "api-root")
    yield app
    _assert_open(app)  # still open in the fixture's own teardown


def test_first_test_gets_an_open_module_app(module_app: Any) -> None:
    _assert_open(module_app)


def test_second_test_still_gets_an_open_module_app(module_app: Any) -> None:
    _assert_open(module_app)


@pytest.fixture(scope="class")
def class_app(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    app = create_app(root=tmp_path_factory.mktemp("class-app") / "api-root")
    yield app
    _assert_open(app)


class TestClassScopedApp:
    def test_first_test_gets_an_open_class_app(self, class_app: Any) -> None:
        _assert_open(class_app)

    def test_second_test_still_gets_an_open_class_app(self, class_app: Any) -> None:
        _assert_open(class_app)
