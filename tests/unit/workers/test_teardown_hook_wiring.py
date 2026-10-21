"""REC-031 F3, F4: the suite teardown hook, exercised through a real pytest session.

Each test runs a child pytest in a subprocess with ``tests.conftest`` loaded as
a plugin, so the hook wiring (not only ``stop_test_workers``) is covered. A
subprocess keeps a deliberately hung thread out of this process.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

REPO_ROOT = Path(__file__).resolve().parents[3]
DEADLINE_S = 1.0  # TEARDOWN_TIMEOUT_S in the child session
MARGIN_S = 3.0
CHILD_TIMEOUT_S = 90.0  # a hang shows up as Pytester.TimeoutExpired

CHILD_CONFTEST = f"""
import json
import threading
import time
from pathlib import Path

import pytest

import tests.conftest as suite_conftest
from recertia.workers.run_worker import AsyncRunWorker

RELEASE = threading.Event()
KEEP = []
TIMES = {{}}


def pytest_configure(config):
    suite_conftest.TEARDOWN_TIMEOUT_S = {DEADLINE_S!r}


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    started = time.monotonic()
    try:
        return (yield)
    finally:
        TIMES[item.name] = time.monotonic() - started
        (Path(__file__).parent / "teardown_times.json").write_text(json.dumps(TIMES))


def pytest_sessionfinish(session):
    RELEASE.set()  # let the hung run end so the child process can exit


@pytest.fixture()
def hung_run():
    worker = AsyncRunWorker(events=None)
    KEEP.append(worker)
    entered = threading.Event()

    def run():
        entered.set()
        RELEASE.wait(600)

    worker._pool.submit(run)
    assert entered.wait(10)
    return worker
"""


def _run_child(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.RunResult:
    paths = [str(REPO_ROOT / "src"), str(REPO_ROOT)]
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(paths))
    pytester.makeconftest(CHILD_CONFTEST)
    return pytester.runpytest_subprocess(
        "-p", "tests.conftest", "-p", "no:cacheprovider", "-v", timeout=CHILD_TIMEOUT_S
    )


def _teardown_times(pytester: pytest.Pytester) -> dict[str, float]:
    times: dict[str, float] = json.loads((pytester.path / "teardown_times.json").read_text())
    return times


def test_hung_run_fails_teardown_within_the_deadline(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1 through the real hook: a hung run is an ERROR at teardown, not a hang.

    Against the unbounded ``shutdown_all`` (``wait=True``) the child blocks in
    teardown until ``CHILD_TIMEOUT_S`` and this test fails with TimeoutExpired.
    """

    pytester.makepyfile(
        test_child="""
        from recertia.workers.run_worker import AsyncRunWorker


        def test_leaves_a_hung_run(hung_run):
            pass


        def test_next_test_is_clean():
            pass


        def test_hung_worker_left_the_registry():
            from conftest import KEEP

            assert KEEP[0] not in AsyncRunWorker._instances
        """
    )
    started = time.monotonic()
    result = _run_child(pytester, monkeypatch)
    wall = time.monotonic() - started

    result.assert_outcomes(passed=3, errors=1)
    result.stdout.fnmatch_lines(
        ["*ERROR at teardown of test_leaves_a_hung_run*", "*async run pool thread did not exit within 1s*"]
    )
    result.stdout.no_fnmatch_line("*previous item was not torn down properly*")
    times = _teardown_times(pytester)
    assert DEADLINE_S * 0.9 <= times["test_leaves_a_hung_run"] < DEADLINE_S + MARGIN_S
    # F2: the straggler's worker left the registry, so later teardowns do not wait on it again.
    assert times["test_next_test_is_clean"] < DEADLINE_S
    assert wall < CHILD_TIMEOUT_S


def test_stuck_queue_worker_fails_teardown_without_a_collateral_error(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3 through the real hook: one ERROR for the leaking test, none for the next."""

    pytester.makepyfile(
        test_child="""
        from recertia.workers.embedded import EmbeddedQueueWorker

        REAL_STOP_ALL = EmbeddedQueueWorker.stop_all.__func__


        def test_leaves_a_stuck_queue_worker(monkeypatch):
            monkeypatch.setattr(EmbeddedQueueWorker, "stop_all", classmethod(lambda cls, timeout=5.0: False))


        def test_next_test_is_clean():
            pass


        def test_monkeypatch_was_undone():
            assert EmbeddedQueueWorker.stop_all.__func__ is REAL_STOP_ALL
        """
    )
    result = _run_child(pytester, monkeypatch)

    result.assert_outcomes(passed=3, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*ERROR at teardown of test_leaves_a_stuck_queue_worker*",
            "*embedded queue worker did not stop within 1s*",
        ]
    )
    result.stdout.no_fnmatch_line("*previous item was not torn down properly*")


def test_error_while_stopping_workers_does_not_cascade(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception while stopping workers is one teardown ERROR, not a session-wide cascade."""

    pytester.makepyfile(
        test_child="""
        from recertia.workers.embedded import EmbeddedQueueWorker


        def _boom(cls, timeout=5.0):
            raise StopIteration


        def test_breaks_stop_all(monkeypatch):
            monkeypatch.setattr(EmbeddedQueueWorker, "stop_all", classmethod(_boom))


        def test_next_test_is_clean():
            pass
        """
    )
    result = _run_child(pytester, monkeypatch)

    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(
        ["*ERROR at teardown of test_breaks_stop_all*", "*stopping test workers raised*"]
    )
    result.stdout.no_fnmatch_line("*previous item was not torn down properly*")
