"""REC-036: a run that never ends must not hang the test process at exit.

``concurrent.futures`` joins every pool thread at interpreter exit, so a hung
``recertia-run`` thread kept the process alive after the session summary until
CI killed the job. ``tests.conftest.session_exit_guard`` calls ``os._exit``
with a nonzero status once the session is over and worker threads are still
alive past ``EXIT_GRACE_S``.

Each test runs a child pytest in a subprocess with ``tests.conftest`` loaded as
a plugin (as in ``test_teardown_hook_wiring.py``). The child records any
``os._exit`` call to a file before exiting, so a clean session can be shown not
to call it. Without the guard the hung child never exits and this test fails
with ``Pytester.TimeoutExpired`` at ``CHILD_TIMEOUT_S``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

REPO_ROOT = Path(__file__).resolve().parents[3]
GRACE_S = 1.0  # EXIT_GRACE_S and TEARDOWN_TIMEOUT_S in the child session
EXIT_LIMIT_S = 30.0  # the hung child must be gone well within this
CHILD_TIMEOUT_S = 60.0  # a hang shows up as Pytester.TimeoutExpired

CHILD_CONFTEST = f"""
import os
import threading
from pathlib import Path

import pytest

import tests.conftest as suite_conftest
from recertia.workers.run_worker import AsyncRunWorker

NEVER = threading.Event()  # never set: the run never ends
KEEP = []
_REAL_EXIT = os._exit


def _recording_exit(status):
    (Path(__file__).parent / "os_exit_status.txt").write_text(str(status))
    _REAL_EXIT(status)


os._exit = _recording_exit


def pytest_configure(config):
    suite_conftest.TEARDOWN_TIMEOUT_S = {GRACE_S!r}
    suite_conftest.EXIT_GRACE_S = {GRACE_S!r}


@pytest.fixture()
def hung_run():
    worker = AsyncRunWorker(events=None)
    KEEP.append(worker)
    entered = threading.Event()

    def run():
        entered.set()
        NEVER.wait()

    worker._pool.submit(run)
    assert entered.wait(10)
    return worker


@pytest.fixture()
def finished_run():
    worker = AsyncRunWorker(events=None)
    worker._pool.submit(lambda: None).result(10)  # leaves an idle recertia-run thread
    return worker
"""


def _run_child(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, test_source: str
) -> tuple[pytest.RunResult, float]:
    paths = [str(REPO_ROOT / "src"), str(REPO_ROOT)]
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(paths))
    pytester.makeconftest(CHILD_CONFTEST)
    pytester.makepyfile(test_child=test_source)
    started = time.monotonic()
    result = pytester.runpytest_subprocess(
        "-p", "tests.conftest", "-p", "no:cacheprovider", "-v", timeout=CHILD_TIMEOUT_S
    )
    return result, time.monotonic() - started


def _os_exit_status(pytester: pytest.Pytester) -> str | None:
    marker = pytester.path / "os_exit_status.txt"
    return marker.read_text() if marker.exists() else None


def test_hung_run_exits_the_session_nonzero_within_the_limit(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hung run is a teardown ERROR, the summary is printed, then the process exits 1."""

    result, wall = _run_child(
        pytester,
        monkeypatch,
        """
        def test_leaves_a_run_that_never_ends(hung_run):
            pass


        def test_next_test_is_clean():
            pass
        """,
    )

    assert result.ret == 1
    assert _os_exit_status(pytester) == "1"
    assert wall < EXIT_LIMIT_S
    result.assert_outcomes(passed=2, errors=1)  # the summary was printed before the exit
    result.stderr.fnmatch_lines(
        ["*recertia exit guard: 1 worker thread(s) still alive 1s after the session ended*recertia-run*"]
    )


def test_exit_status_is_nonzero_even_when_every_test_passed(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker thread the teardown hook cannot see still fails the process (status 1, not 0)."""

    result, wall = _run_child(
        pytester,
        monkeypatch,
        """
        import threading

        from conftest import NEVER


        def test_starts_a_bare_worker_thread():
            threading.Thread(target=NEVER.wait, name="recertia-queue-bare").start()
        """,
    )

    assert result.ret == 1
    assert _os_exit_status(pytester) == "1"
    assert wall < EXIT_LIMIT_S
    result.assert_outcomes(passed=1)
    result.stderr.fnmatch_lines(["*recertia exit guard*recertia-queue-bare*"])


def test_clean_session_exits_zero_without_os_exit(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session whose runs all ended exits 0 normally; the guard never calls ``os._exit``."""

    result, wall = _run_child(
        pytester,
        monkeypatch,
        """
        def test_runs_to_completion(finished_run):
            pass


        def test_plain():
            pass
        """,
    )

    assert result.ret == 0
    assert _os_exit_status(pytester) is None
    assert wall < EXIT_LIMIT_S
    result.assert_outcomes(passed=2)
    result.stderr.no_fnmatch_line("*recertia exit guard*")
