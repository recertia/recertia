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
CHILD_TIMEOUT_S = 45.0  # a hang shows up as Pytester.TimeoutExpired, before the 60s pytest-timeout
STUCK_S = 15.0  # a bounded stuck thread: without the guard the child exits 0 after this

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
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    test_source: str,
    *,
    plugins_first: tuple[str, ...] = (),
) -> tuple[pytest.RunResult, float]:
    """Run ``test_source`` in a child pytest; ``plugins_first`` are registered before tests.conftest."""

    paths = [str(REPO_ROOT / "src"), str(REPO_ROOT), str(pytester.path)]
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(paths))
    pytester.makeconftest(CHILD_CONFTEST)
    pytester.makepyfile(test_child=test_source)
    first = [arg for name in plugins_first for arg in ("-p", name)]
    started = time.monotonic()
    result = pytester.runpytest_subprocess(
        *first, "-p", "tests.conftest", "-p", "no:cacheprovider", "-v", timeout=CHILD_TIMEOUT_S
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
        ["*recertia exit guard: 1 worker thread(s) still alive past the 1s exit grace*recertia-run*"]
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


# --- Nested in-process sessions (REC-036) ------------------------------------------
# The guard must run for the outermost session only. pytest unconfigures a config
# whose pytest_configure raised before tests.conftest's ran, so a session counter
# could drift: an inner session failing during configure either fired the guard
# inside the inner session (os._exit of the whole process mid-test) or left the
# outer session without a guard. The outer session here is a pytester child; each
# of its tests runs an inner session in-process with tests.conftest loaded.

NESTED_SOURCE = """
import threading

import pytest

pytest_plugins = ["pytester"]

FAILING_CONFTEST = "def pytest_configure(config):\\n    raise RuntimeError('inner configure failed')\\n"


@pytest.fixture()
def live_worker():
    release = threading.Event()
    thread = threading.Thread(target=release.wait, name="recertia-queue-outer", daemon=True)
    thread.start()
    yield thread
    release.set()
    thread.join(10)


def run_inner(pytester, conftest=None):
    if conftest is not None:
        pytester.makeconftest(conftest)
    pytester.makepyfile(test_inner="def test_inner():\\n    pass\\n")
    return pytester.runpytest_inprocess("-p", "tests.conftest", "-p", "no:cacheprovider")


def run_failing_inner(pytester):
    try:
        result = run_inner(pytester, FAILING_CONFTEST)
    except RuntimeError as exc:
        assert "inner configure failed" in str(exc)
    else:
        assert result.ret != 0
"""


def test_nested_sessions_do_not_run_the_guard_and_the_outer_guard_still_fires(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal inner session and one failing during configure, both with a live worker thread.

    Neither inner session may run the guard (it would see the outer thread and
    ``os._exit`` the outer process mid-test, with no summary), and the outer
    session's own guard still fires for the stuck thread its last test leaves.
    """

    result, wall = _run_child(
        pytester,
        monkeypatch,
        NESTED_SOURCE
        + """

def test_normal_inner_session(pytester, live_worker):
    assert run_inner(pytester).ret == 0
    assert live_worker.is_alive()


def test_inner_session_fails_during_configure(pytester, live_worker):
    run_failing_inner(pytester)
    assert live_worker.is_alive()


def test_leaves_a_stuck_worker():
    from conftest import NEVER

    threading.Thread(target=NEVER.wait, name="recertia-queue-stuck").start()
""",
    )

    result.assert_outcomes(passed=3)  # no os._exit mid-session: the summary was printed
    assert result.ret == 1
    assert _os_exit_status(pytester) == "1"
    assert wall < EXIT_LIMIT_S
    result.stderr.fnmatch_lines(["*recertia exit guard: 1 worker thread(s)*recertia-queue-stuck*"])


def test_outer_guard_fires_after_an_inner_session_failed_during_configure(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no live thread during the failing inner session, the outer guard must still run.

    The stuck thread ends on its own after ``STUCK_S``, so a missing guard shows
    up as rc 0 with no guard line instead of a hang.
    """

    result, wall = _run_child(
        pytester,
        monkeypatch,
        NESTED_SOURCE
        + f"""

def test_inner_session_fails_during_configure(pytester):
    run_failing_inner(pytester)


def test_leaves_a_stuck_worker():
    threading.Thread(target=threading.Event().wait, args=({STUCK_S!r},), name="recertia-queue-stuck").start()
""",
    )

    result.assert_outcomes(passed=2)
    assert result.ret == 1
    assert _os_exit_status(pytester) == "1"
    assert wall < STUCK_S
    result.stderr.fnmatch_lines(["*recertia exit guard*recertia-queue-stuck*"])


# --- Unconfigure order and exit status (REC-036) --------------------------------

HUNG_SOURCE = """
def test_leaves_a_run_that_never_ends(hung_run):
    pass
"""


def test_plugins_registered_earlier_unconfigure_before_the_guard_exits(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``trylast`` on the guard's unconfigure lets earlier plugins finish first.

    Hooks run in reverse registration order, so without ``trylast`` the guard
    (registered after ``early_marker``) would ``os._exit`` before
    ``early_marker``'s unconfigure ran, as it would before a log-file close.
    """

    pytester.makepyfile(
        early_marker="""
        from pathlib import Path


        def pytest_unconfigure(config):
            (Path(str(config.rootpath)) / "early_unconfigure.txt").write_text("ran")
        """
    )
    result, wall = _run_child(pytester, monkeypatch, HUNG_SOURCE, plugins_first=("early_marker",))

    assert result.ret == 1
    assert _os_exit_status(pytester) == "1"
    assert wall < EXIT_LIMIT_S
    assert (pytester.path / "early_unconfigure.txt").read_text() == "ran"


def test_nonzero_session_status_is_kept_by_the_guard(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session status other than 0 is passed to ``os._exit`` as is, not forced to 1."""

    result, wall = _run_child(
        pytester,
        monkeypatch,
        HUNG_SOURCE
        + """

def test_then_exit_with_status_3():
    import pytest

    pytest.exit("stop", returncode=3)
""",
    )

    assert result.ret == 3
    assert _os_exit_status(pytester) == "3"
    assert wall < EXIT_LIMIT_S
    result.stderr.fnmatch_lines(["*recertia exit guard*os._exit(3)*"])


def test_guard_fires_with_the_network_guard_opted_out(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``RECERTIA_NO_NETWORK=0`` (CI unset) skips the network guard, never the exit guard.

    ``_outermost_config`` must be set before the opt-out return in
    ``pytest_configure``. The stuck thread ends after ``STUCK_S``, so a missing
    guard shows up as rc 0 instead of a hang.
    """

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("RECERTIA_NO_NETWORK", "0")
    result, wall = _run_child(
        pytester,
        monkeypatch,
        f"""
import socket
import threading

import tests.conftest as suite_conftest


def test_network_guard_is_off_and_leaves_a_stuck_worker():
    assert not suite_conftest.network_guard_enabled()
    assert socket.getaddrinfo is suite_conftest._REAL["getaddrinfo"]
    threading.Thread(target=threading.Event().wait, args=({STUCK_S!r},), name="recertia-queue-stuck").start()
""",
    )

    result.assert_outcomes(passed=1)
    assert result.ret == 1
    assert _os_exit_status(pytester) == "1"
    assert wall < STUCK_S
    result.stderr.fnmatch_lines(["*recertia exit guard*recertia-queue-stuck*"])


# --- Network guard across nested in-process sessions (REC-042) -------------------------
# Every unconfigure used to restore the real socket functions, so an inner in-process
# session with tests.conftest switched the network guard off for every later test of
# the outer session. Only the outermost session may restore them. Nothing here reaches
# the network: the real socket functions are replaced by tripwires while a test runs,
# and the target is TEST-NET-1 (RFC 5737), which is never routable.

NETGUARD_SOURCE = """
import socket

import pytest

import tests.conftest as suite_conftest
from tests.conftest import NetworkBlockedError

pytest_plugins = ["pytester"]

TARGET = ("192.0.2.1", 9)
GUARDED = {
    "connect": suite_conftest._guarded_connect,
    "connect_ex": suite_conftest._guarded_connect_ex,
    "getaddrinfo": suite_conftest._guarded_getaddrinfo,
    "create_connection": suite_conftest._guarded_create_connection,
}
REAL = dict(suite_conftest._REAL)  # the real functions, before any inner session
FAILING_CONFTEST = "def pytest_configure(config):\\n    raise RuntimeError('inner configure failed')\\n"


class RealSocketReached(AssertionError):
    pass


@pytest.fixture()
def tripwire(monkeypatch):
    def trip(*args, **kwargs):
        raise RealSocketReached(f"a real socket function was reached: {args!r}")

    for name in list(suite_conftest._REAL):
        monkeypatch.setitem(suite_conftest._REAL, name, trip)


def current():
    return {
        "connect": socket.socket.connect,
        "connect_ex": socket.socket.connect_ex,
        "getaddrinfo": socket.getaddrinfo,
        "create_connection": socket.create_connection,
    }


def assert_guard_blocks():
    assert current() == GUARDED  # the same guarded functions: installed once, not wrapped
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(NetworkBlockedError):
            sock.connect(TARGET)
        with pytest.raises(NetworkBlockedError):
            sock.connect_ex(TARGET)
    with pytest.raises(NetworkBlockedError):
        socket.getaddrinfo(TARGET[0], TARGET[1])
    with pytest.raises(NetworkBlockedError):
        socket.create_connection(TARGET, timeout=0.1)


def run_inner_sessions(pytester):
    pytester.makepyfile(test_inner="def test_inner():\\n    pass\\n")
    assert pytester.runpytest_inprocess("-p", "tests.conftest", "-p", "no:cacheprovider").ret == 0
    pytester.makeconftest(FAILING_CONFTEST)
    try:
        result = pytester.runpytest_inprocess("-p", "tests.conftest", "-p", "no:cacheprovider")
    except RuntimeError as exc:
        assert "inner configure failed" in str(exc)
    else:
        assert result.ret != 0
"""


def _run_netguard_child(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, test_source: str
) -> pytest.RunResult:
    monkeypatch.setenv("RECERTIA_NO_NETWORK", "1")
    result, wall = _run_child(pytester, monkeypatch, NETGUARD_SOURCE + test_source)
    assert wall < EXIT_LIMIT_S
    return result


def test_inner_in_process_session_keeps_the_outer_network_guard(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a normal and a configure-failing inner session, the outer guard still blocks."""

    result = _run_netguard_child(
        pytester,
        monkeypatch,
        """

def test_guard_blocks_before_any_inner_session(tripwire):
    assert_guard_blocks()


def test_inner_sessions(pytester, tripwire):
    run_inner_sessions(pytester)


def test_guard_still_blocks_after_the_inner_sessions(tripwire):
    assert_guard_blocks()


def test_real_functions_are_not_guarded_ones():
    assert suite_conftest._REAL == REAL
    assert not set(map(id, REAL.values())) & set(map(id, GUARDED.values()))
""",
    )

    result.assert_outcomes(passed=4)
    assert result.ret == 0


def test_socket_restore_and_exit_guard_run_for_the_outermost_config_only(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inner sessions neither restore the sockets nor run the guard; the outermost does both, in order."""

    result = _run_netguard_child(
        pytester,
        monkeypatch,
        """
from pathlib import Path

LOG = Path(__file__).parent / "guard_calls.txt"
OUTER = []
_real_guard = suite_conftest.session_exit_guard


def recording_guard(config):
    with LOG.open("a") as fh:
        fh.write(f"guard outer={config is OUTER[0]} restored={current() == REAL}\\n")
    _real_guard(config)


suite_conftest.session_exit_guard = recording_guard


def test_outer_config(request):
    OUTER.append(request.config)
    assert suite_conftest._outermost_config is request.config


def test_inner_sessions_restore_nothing(pytester, request):
    run_inner_sessions(pytester)
    assert not LOG.exists()  # no inner session ran the guard
    assert current() == GUARDED  # nor restored the real socket functions
    assert suite_conftest._REAL == REAL  # nor re-captured guarded ones as "real"
    assert suite_conftest._outermost_config is request.config
""",
    )

    result.assert_outcomes(passed=2)
    assert result.ret == 0
    assert (pytester.path / "guard_calls.txt").read_text() == "guard outer=True restored=True\n"
