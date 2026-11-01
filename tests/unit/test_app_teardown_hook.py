"""The app-state teardown in ``tests/conftest.py``, exercised through a real pytest session.

A child pytest runs in a subprocess with ``tests.conftest`` loaded as a plugin, as in
``tests/unit/workers/test_teardown_hook_wiring.py``, so the hook wiring is covered, not
only the helper it calls. The child tests record each app's queue connection in the
child conftest and later tests check whether it was closed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

REPO_ROOT = Path(__file__).resolve().parents[2]
CHILD_TIMEOUT_S = 45.0

CHILD_CONFTEST = """
import sqlite3

OPENED = {}


def is_open(conn):
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return False
    return True
"""

CHILD_TESTS = """
import pytest
from fastapi.testclient import TestClient

from conftest import OPENED, is_open
from recertia.api import create_app


def _queue_conn(app):
    return app.state.recertia.queue._conn


def _raising_close(tmp_path, message):
    state = create_app(root=tmp_path / "api-root").state.recertia
    real = state.queue.close

    def close():
        real()
        raise RuntimeError(message)

    state.queue.close = close  # not monkeypatch: that is undone before the app is closed


def test_bare_client_app(tmp_path):
    app = create_app(root=tmp_path / "api-root")
    TestClient(app)  # no ``with``: the lifespan never runs
    OPENED["bare"] = _queue_conn(app)
    assert is_open(OPENED["bare"])


def test_bare_client_app_was_closed():
    assert not is_open(OPENED["bare"])


@pytest.fixture()
def fixture_app(tmp_path):
    app = create_app(root=tmp_path / "api-root")
    OPENED["fixture"] = _queue_conn(app)
    yield app
    OPENED["fixture_open_in_finalizer"] = is_open(_queue_conn(app))


def test_fixture_app(fixture_app):
    assert is_open(_queue_conn(fixture_app))


def test_fixture_app_was_open_in_its_finalizer_and_closed_after():
    assert OPENED["fixture_open_in_finalizer"] is True
    assert not is_open(OPENED["fixture"])


def test_raising_close_is_a_teardown_error(tmp_path):
    _raising_close(tmp_path, "close boom")


def test_call_failure_stays_visible(tmp_path):
    _raising_close(tmp_path, "close boom after a failure")
    assert False, "the call phase failed"


@pytest.fixture(scope="module")
def module_app(tmp_path_factory):
    app = create_app(root=tmp_path_factory.mktemp("module") / "api-root")
    OPENED["module"] = _queue_conn(app)
    yield app
    OPENED["module_open_in_finalizer"] = is_open(_queue_conn(app))


def test_module_app_first(module_app):
    assert is_open(_queue_conn(module_app))


def test_module_app_second(module_app):
    assert is_open(_queue_conn(module_app))
"""

CHILD_AFTER = """
from conftest import OPENED, is_open


def test_module_app_was_open_in_its_finalizer_and_closed_after():
    assert OPENED["module_open_in_finalizer"] is True
    assert not is_open(OPENED["module"])
"""


def test_teardown_closes_each_app_at_the_end_of_its_scope(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real hooks: what is closed, when, and how a failing close is reported.

    - an app behind a bare ``TestClient`` is closed after its test;
    - a fixture's app is still open in the fixture's finalizer, and closed after it;
    - a raising close is a teardown ERROR, and a call-phase failure is still reported;
    - a module-scoped fixture's app stays open for its second test and its own
      finalizer, and is closed when the module ends.
    """

    paths = [str(REPO_ROOT / "src"), str(REPO_ROOT)]
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(paths))
    pytester.makeconftest(CHILD_CONFTEST)
    pytester.makepyfile(test_a_child=CHILD_TESTS, test_b_after=CHILD_AFTER)
    result = pytester.runpytest_subprocess(
        "-p", "tests.conftest", "-p", "no:cacheprovider", "-v", timeout=CHILD_TIMEOUT_S
    )

    result.assert_outcomes(passed=8, failed=1, errors=2)
    result.stdout.fnmatch_lines(
        [
            "*test_a_child.py::test_raising_close_is_a_teardown_error PASSED*",
            "*test_a_child.py::test_raising_close_is_a_teardown_error ERROR*",
            "*test_a_child.py::test_call_failure_stays_visible FAILED*",
            "*test_a_child.py::test_call_failure_stays_visible ERROR*",
        ],
        consecutive=False,
    )
    result.stdout.fnmatch_lines(["*closing an app state raised RuntimeError('close boom') in teardown*"])
    result.stdout.fnmatch_lines(
        ["*closing an app state raised RuntimeError('close boom after a failure') in teardown*"]
    )
    result.stdout.fnmatch_lines(["*AssertionError: the call phase failed*"])
    result.stdout.no_fnmatch_line("*previous item was not torn down properly*")
