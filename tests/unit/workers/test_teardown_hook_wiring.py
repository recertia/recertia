"""REC-031 F3, F4: the suite teardown hook, exercised through a real pytest session.

Each test runs a child pytest in a subprocess with ``tests.conftest`` loaded as
a plugin, so the hook wiring (not only ``stop_test_workers``) is covered. A
subprocess keeps a deliberately hung thread out of this process.

Loading the conftest with ``-p`` registers it earlier than a real
``conftest.py``. The child's hook order (logging, capture, the conftest
wrapper, runner) matches the suite's only because the wrapper is ``trylast``;
without it the suite's wrapper sorts outside capture while the child's stays
inside, so these child tests cannot catch a ``trylast`` regression (CTO-rec
D1, D2). ``test_teardown_wrapper_is_trylast_inside_capture`` checks it in this
process, where the conftest is loaded as a real ``conftest.py``.
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
CHILD_TIMEOUT_S = 45.0  # a hang shows up as Pytester.TimeoutExpired, before the 60s pytest-timeout
TEST_BUDGET_S = 2.0  # pytest-timeout in the D2 child session
STOP_HANG_S = 10.0  # a worker stop that outlives that budget

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


@pytest.fixture()
def finalizer_sees_no_workers():
    worker = AsyncRunWorker(events=None)
    worker._pool.submit(lambda: None).result(10)  # an idle recertia-run thread
    yield worker
    alive = [
        t.name
        for t in threading.enumerate()
        if t.name.startswith(("recertia-run", "recertia-queue")) and t.is_alive()
    ]
    assert alive == [], "worker threads alive in a fixture finalizer: " + repr(alive)
"""


def _run_child(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, *args: str) -> pytest.RunResult:
    paths = [str(REPO_ROOT / "src"), str(REPO_ROOT)]
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(paths))
    pytester.makeconftest(CHILD_CONFTEST)
    return pytester.runpytest_subprocess(
        "-p", "tests.conftest", "-p", "no:cacheprovider", "-v", *args, timeout=CHILD_TIMEOUT_S
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


def test_timeout_while_stopping_workers_does_not_skip_teardown(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REC-044 D2: a pytest-timeout while stopping workers is one teardown ERROR, not a cascade.

    pytest-timeout's signal method raises ``pytest.fail.Exception``, a
    BaseException, wherever the main thread is. Landing in the worker stop, it
    must not leave the wrapper before its ``yield``: that skips pytest's own
    teardown (here the monkeypatch undo), so the next test errors with
    "previous item was not torn down properly" and the stuck stop runs again.
    """

    pytester.makepyfile(
        test_child=f"""
        import time

        from recertia.workers.embedded import EmbeddedQueueWorker

        REAL_STOP_ALL = EmbeddedQueueWorker.stop_all.__func__


        def _outlives_the_budget(cls, timeout=5.0):
            time.sleep({STOP_HANG_S!r})  # pytest-timeout interrupts this
            return True


        def test_stop_outlives_the_budget(monkeypatch):
            monkeypatch.setattr(EmbeddedQueueWorker, "stop_all", classmethod(_outlives_the_budget))


        def test_next_test_is_clean():
            pass


        def test_monkeypatch_was_undone():
            assert EmbeddedQueueWorker.stop_all.__func__ is REAL_STOP_ALL
        """
    )
    result = _run_child(
        pytester, monkeypatch, "-o", f"timeout={TEST_BUDGET_S:g}", "-o", "timeout_method=signal"
    )

    result.assert_outcomes(passed=3, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*ERROR at teardown of test_stop_outlives_the_budget*",
            f"*stopping test workers raised*Timeout (>{TEST_BUDGET_S:g}*s) from pytest-timeout*",
        ]
    )
    result.stdout.no_fnmatch_line("*previous item was not torn down properly*")


def test_workers_are_stopped_before_fixture_finalizers(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CTO-rec S2: fixture finalizers run with every worker thread already stopped.

    Against "yield first, then stop", the finalizer sees the live pool thread
    and the test ERRORs at teardown.
    """

    pytester.makepyfile(
        test_child="""
        def test_uses_a_pool(finalizer_sees_no_workers):
            pass
        """
    )
    result = _run_child(pytester, monkeypatch)

    result.assert_outcomes(passed=1)
    result.stdout.no_fnmatch_line("*worker threads alive in a fixture finalizer*")


def test_finalizer_error_keeps_the_leak_message(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CTO-rec S1b: when a finalizer raises, the leak message rides on that error as a note."""

    pytester.makepyfile(
        test_child="""
        import pytest

        from recertia.workers.embedded import EmbeddedQueueWorker


        @pytest.fixture()
        def boom():
            yield
            raise RuntimeError("finalizer boom")


        def test_leaks_and_finalizer_raises(monkeypatch, boom):
            monkeypatch.setattr(EmbeddedQueueWorker, "stop_all", classmethod(lambda cls, timeout=5.0: False))


        def test_next_test_is_clean():
            pass
        """
    )
    result = _run_child(pytester, monkeypatch)

    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*ERROR at teardown of test_leaks_and_finalizer_raises*",
            "*RuntimeError: finalizer boom*",
            "*embedded queue worker did not stop within 1s*",
        ]
    )
    result.stdout.no_fnmatch_line("*previous item was not torn down properly*")


def test_teardown_wrapper_is_trylast_inside_capture(request: pytest.FixtureRequest) -> None:
    """CTO-rec D1: the suite's teardown wrapper is ``trylast`` and sorts inside capturemanager.

    In-process, so it sees the real ``conftest.py`` registration order. Without
    ``trylast`` the wrapper sorts outside capture and output printed while
    stopping workers escapes the test's captured teardown section.
    """

    conftest_path = (REPO_ROOT / "tests" / "conftest.py").resolve()
    hook = request.config.pluginmanager.hook.pytest_runtest_teardown
    impls = hook.get_hookimpls()  # call order is reversed: the last entry is outermost
    ours = [i for i in impls if Path(i.function.__code__.co_filename).resolve() == conftest_path]
    capture = [i for i in impls if i.plugin_name == "capturemanager"]
    assert len(ours) == 1, [i.plugin_name for i in impls]
    assert len(capture) == 1, [i.plugin_name for i in impls]
    assert ours[0].wrapper and ours[0].trylast
    assert impls.index(ours[0]) < impls.index(capture[0]), [i.plugin_name for i in impls]
