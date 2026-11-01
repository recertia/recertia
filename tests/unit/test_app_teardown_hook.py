"""The app-state teardown in ``tests/conftest.py``, exercised through a real pytest session.

A child pytest runs in a subprocess with ``tests.conftest`` loaded as a plugin, as in
``tests/unit/workers/test_teardown_hook_wiring.py``, so the hook wiring is covered, not
only the helper it calls. The child tests record each app's queue connection in the
child conftest and later tests check whether it was closed.
"""

from __future__ import annotations

import json
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


def _run_child(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.RunResult:
    paths = [str(REPO_ROOT / "src"), str(REPO_ROOT)]
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(paths))
    return pytester.runpytest_subprocess(
        "-p", "tests.conftest", "-p", "no:cacheprovider", "-v", timeout=CHILD_TIMEOUT_S
    )


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

    pytester.makeconftest(CHILD_CONFTEST)
    pytester.makepyfile(test_a_child=CHILD_TESTS, test_b_after=CHILD_AFTER)
    result = _run_child(pytester, monkeypatch)

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


# Every scope the teardown keys on. The session and package apps are used across two
# modules; the child conftest counts each app's closes and writes them at session end.
SCOPES_CONFTEST = """
import json
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from recertia.api import create_app
from recertia.app.state import AppState

OPENED = {}
NAMES = {}
CLOSES = Counter()
_real_close = AppState.close


def _counted_close(self):
    CLOSES[NAMES.get(id(self), "other")] += 1
    return _real_close(self)


AppState.close = _counted_close


def is_open(conn):
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return False
    return True


def build_app(root, name):
    app = create_app(root=root / "api-root")
    NAMES[id(app.state.recertia)] = name
    OPENED[name] = app.state.recertia.queue._conn
    return app


@pytest.fixture(scope="session")
def session_app(tmp_path_factory):
    app = build_app(tmp_path_factory.mktemp("session"), "session")
    yield app
    OPENED["session_open_in_finalizer"] = is_open(OPENED["session"])


def pytest_sessionfinish(session):
    names = ("session", "package", "body", "module_f")
    report = {
        "open_at_end": {name: is_open(OPENED[name]) for name in names if name in OPENED},
        "closes": dict(CLOSES),
        "session_open_in_finalizer": OPENED.get("session_open_in_finalizer"),
    }
    (Path(__file__).parent / "scopes_report.json").write_text(json.dumps(report))
"""

PACKAGE_CONFTEST = """
import pytest

from conftest import OPENED, build_app, is_open


@pytest.fixture(scope="package")
def package_app(tmp_path_factory):
    app = build_app(tmp_path_factory.mktemp("package"), "package")
    yield app
    OPENED["package_open_in_finalizer"] = is_open(OPENED["package"])
"""

PACKAGE_FIRST = """
from conftest import OPENED, is_open


def test_package_and_session_apps_open_in_the_first_module(package_app, session_app):
    assert is_open(OPENED["package"])
    assert is_open(OPENED["session"])
"""

PACKAGE_SECOND = """
from conftest import OPENED, is_open


def test_package_and_session_apps_still_open_in_the_second_module(package_app, session_app):
    assert is_open(OPENED["package"])
    assert is_open(OPENED["session"])
"""

AFTER_PACKAGE = """
from conftest import OPENED, is_open


def test_package_app_closed_after_its_package_and_session_app_still_open(session_app):
    assert OPENED["package_open_in_finalizer"] is True
    assert not is_open(OPENED["package"])
    assert is_open(OPENED["session"])
"""

RAISING_MODULE = """
import pytest

from conftest import build_app


@pytest.fixture(scope="module")
def raising_module_app(tmp_path_factory):
    app = build_app(tmp_path_factory.mktemp("raising"), "raising_module")
    state = app.state.recertia
    real = state.queue.close

    def close():
        real()
        raise RuntimeError("module close boom")

    state.queue.close = close
    return app


def test_raising_module_app_first(raising_module_app):
    pass


def test_raising_module_app_last(raising_module_app):
    pass
"""

BODY_AFTER_MODULE = """
import pytest

from conftest import OPENED, build_app, is_open


@pytest.fixture(scope="module")
def module_app_f(tmp_path_factory):
    return build_app(tmp_path_factory.mktemp("module_f"), "module_f")


def test_body_app_built_after_a_module_fixture(module_app_f, tmp_path_factory):
    build_app(tmp_path_factory.mktemp("body"), "body")
    assert is_open(OPENED["body"])


def test_body_app_was_closed_after_its_own_test(module_app_f):
    assert not is_open(OPENED["body"])
    assert is_open(OPENED["module_f"])
"""


def test_teardown_closes_session_package_and_module_apps_once_at_scope_end(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real hooks, for every wider scope the teardown keys on.

    - a session app and a package app stay open across two modules and in their own
      finalizers; the package app is closed when its package ends, the session app at
      session end, each exactly once;
    - a module fixture whose app's close raises gives a teardown ERROR on the module's
      last test;
    - an app built in a test body after a module fixture was set up belongs to that
      test, so it is closed after the test, not when the module ends.
    """

    pytester.makeconftest(SCOPES_CONFTEST)
    package = pytester.mkpydir("test_c_package")
    (package / "conftest.py").write_text(PACKAGE_CONFTEST)
    (package / "test_first.py").write_text(PACKAGE_FIRST)
    (package / "test_second.py").write_text(PACKAGE_SECOND)
    pytester.makepyfile(
        test_d_after_package=AFTER_PACKAGE,
        test_e_raising_module=RAISING_MODULE,
        test_f_body_after_module=BODY_AFTER_MODULE,
    )
    result = _run_child(pytester, monkeypatch)

    result.assert_outcomes(passed=7, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*test_e_raising_module.py::test_raising_module_app_last PASSED*",
            "*test_e_raising_module.py::test_raising_module_app_last ERROR*",
            "*closing an app state raised RuntimeError('module close boom') in teardown*",
        ],
        consecutive=False,
    )
    result.stdout.no_fnmatch_line("*test_raising_module_app_first ERROR*")
    result.stdout.no_fnmatch_line("*previous item was not torn down properly*")
    report = json.loads((pytester.path / "scopes_report.json").read_text())
    assert report["session_open_in_finalizer"] is True
    assert report["open_at_end"] == {"session": False, "package": False, "body": False, "module_f": False}
    assert report["closes"] == {"session": 1, "package": 1, "raising_module": 1, "body": 1, "module_f": 1}


# The review's probe 2: a module fixture's own teardown code restarts its app's queue
# worker on an item that outlives the bounded stop, after the pre-yield ``stop_all``.
BUSY_AFTER_FIXTURE_TEARDOWN = """
import threading
import time

import pytest

from conftest import OPENED, build_app, is_open

BUSY_S = 2.0


@pytest.fixture(scope="module")
def busy_module_app(tmp_path_factory):
    app = build_app(tmp_path_factory.mktemp("busy"), "busy")
    yield app
    embedded = app.state.recertia.embedded

    def busy_loop(stop):
        try:
            time.sleep(BUSY_S)  # one long item, past the bounded stop
        finally:
            embedded._exited(threading.current_thread())

    embedded._loop = busy_loop
    embedded.ensure_started()


def test_busy_module_app_first(busy_module_app):
    pass


def test_busy_module_app_last(busy_module_app):
    pass
"""

NEXT_MODULE = """
def test_next_test_is_clean():
    pass
"""


def test_a_worker_busy_after_fixture_teardown_is_reported_on_that_test(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker still running an item after the bounded stop is a teardown ERROR.

    It goes on the test whose teardown ran the fixture's finalizer (the module's last
    test), not silently with only a log warning and not on the next test.
    """

    pytester.makeconftest(SCOPES_CONFTEST)
    pytester.makepyfile(test_busy=BUSY_AFTER_FIXTURE_TEARDOWN, test_busy_next=NEXT_MODULE)
    result = _run_child(pytester, monkeypatch)

    result.assert_outcomes(passed=3, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*test_busy.py::test_busy_module_app_last PASSED*",
            "*test_busy.py::test_busy_module_app_last ERROR*",
            "*app state left open in teardown: its queue worker is still running an item*",
        ],
        consecutive=False,
    )
    result.stdout.no_fnmatch_line("*test_next_test_is_clean ERROR*")
    result.stdout.no_fnmatch_line("*did not stop within 5s*")
    result.stdout.no_fnmatch_line("*previous item was not torn down properly*")
