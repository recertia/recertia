"""Explicit test-only execution capability for integration fixtures.

Production defaults to the Docker/Podman backend.  Tests opt into the bounded
local executor before importing or constructing orchestration services.
"""

from __future__ import annotations

import os

os.environ.setdefault("RECERTIA_EXECUTION_BACKEND", "local")
# Tests exercise the HTTP API against the local backend; production API refuses local
# unless this break-glass flag is set.
os.environ.setdefault("RECERTIA_API_ALLOW_LOCAL_EXEC", "1")
# CI / non-Windows hosts: allow POSIX absolute registered roots (RW0 tests).
os.environ.setdefault("RECERTIA_ALLOW_POSIX_WORKSPACE_ROOTS", "1")
# Busy-lock import waits then raises. Keep the wait short in the suite.
os.environ.setdefault("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")


# --- Repo-wide no-external-network guard (PR5) --------------------------------------
# Installed for the whole session at configure time, so it also covers import-time and
# fixture-time connections, not just test bodies. Always on in CI (CI=true or
# RECERTIA_NO_NETWORK=1); locally, RECERTIA_NO_NETWORK=0 opts out for ad-hoc debugging.
# Loopback and AF_UNIX stay allowed (TestClient, local servers, Docker socket).

import functools  # noqa: E402
import ipaddress  # noqa: E402
import socket  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import weakref  # noqa: E402
from collections.abc import Generator  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", ""}
_REAL: dict[str, Any] = {
    "connect": socket.socket.connect,
    "connect_ex": socket.socket.connect_ex,
    "getaddrinfo": socket.getaddrinfo,
    "create_connection": socket.create_connection,
}


class NetworkBlockedError(OSError):
    """A test tried to reach a non-loopback host (OSError so urllib wraps it as URLError)."""


def network_guard_enabled() -> bool:
    if os.environ.get("CI", "").lower() == "true":
        return True
    return os.environ.get("RECERTIA_NO_NETWORK", "1") != "0"


def _is_loopback(host: Any) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host is None or str(host).lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _check_addr(sock: socket.socket, addr: Any) -> None:
    if sock.family == getattr(socket, "AF_UNIX", object()):
        return
    host = addr[0] if isinstance(addr, tuple) and addr else addr
    if not _is_loopback(host):
        raise NetworkBlockedError(f"external network blocked in tests: {addr!r}")


def _guarded_connect(self: socket.socket, addr: Any) -> None:
    _check_addr(self, addr)
    return _REAL["connect"](self, addr)


def _guarded_connect_ex(self: socket.socket, addr: Any) -> int:
    _check_addr(self, addr)
    return _REAL["connect_ex"](self, addr)


def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    if not _is_loopback(host):
        raise NetworkBlockedError(f"external DNS lookup blocked in tests: {host!r}")
    return _REAL["getaddrinfo"](host, *args, **kwargs)


def _guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
    if not _is_loopback(address[0]):
        raise NetworkBlockedError(f"external network blocked in tests: {address!r}")
    return _REAL["create_connection"](address, *args, **kwargs)


def pytest_configure(config: Any) -> None:
    global _outermost_config
    if _outermost_config is None:  # first config wins; nested in-process sessions never replace it
        _outermost_config = config
    if not network_guard_enabled():
        return
    setattr(socket.socket, "connect", _guarded_connect)
    setattr(socket.socket, "connect_ex", _guarded_connect_ex)
    setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    setattr(socket, "create_connection", _guarded_create_connection)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config: Any) -> None:
    global _outermost_config
    # The outermost session only, never a nested in-process one. Keyed on the config
    # object, not a counter: pytest unconfigures a config whose pytest_configure
    # failed before ours ran, so a counter could drift (REC-036).
    if config is _outermost_config:
        _outermost_config = None
        # Restore the real socket functions here only: a nested in-process session
        # (pytester with -p tests.conftest) must not switch off the outer session's
        # network guard. Its configure re-sets the same functions, so none is double-wrapped.
        setattr(socket.socket, "connect", _REAL["connect"])
        setattr(socket.socket, "connect_ex", _REAL["connect_ex"])
        setattr(socket, "getaddrinfo", _REAL["getaddrinfo"])
        setattr(socket, "create_connection", _REAL["create_connection"])
        session_exit_guard(config)


# --- Session exit guard (REC-036) -----------------------------------------------------
# A run that never ends keeps its ``recertia-run`` pool thread alive after the session.
# concurrent.futures joins every pool thread at interpreter exit (daemon or not), so the
# process would hang after printing its summary until CI kills the job. The teardown
# hook already reports the leak as an ERROR; this guard makes the process exit too.

# Grace for worker threads still alive at session end. A module attribute so the
# pytester exit-guard test can lower it in a child session.
EXIT_GRACE_S = 5.0
WORKER_THREAD_PREFIXES = ("recertia-run", "recertia-queue")
_EXIT_STATUS = pytest.StashKey[int]()
_outermost_config: Any = None  # the config of the outermost session (REC-036)


def pytest_sessionfinish(session: Any, exitstatus: Any) -> None:
    session.config.stash[_EXIT_STATUS] = int(exitstatus)


def lingering_worker_threads() -> list[threading.Thread]:
    return [
        t
        for t in threading.enumerate()
        if t is not threading.current_thread() and t.is_alive() and t.name.startswith(WORKER_THREAD_PREFIXES)
    ]


def session_exit_guard(config: Any) -> None:
    """``os._exit`` after the report if worker threads are still alive.

    Pool threads would block interpreter exit; a daemon ``recertia-queue``
    thread would not, but a leaked one still fails the process.

    Runs at the end of ``pytest_unconfigure`` (trylast), after the terminal
    summary. A clean session has no lingering worker thread and returns at
    once. Otherwise registered pools are shut down and queue workers stopped
    (so idle pool threads end), every lingering thread is joined against one
    ``EXIT_GRACE_S`` deadline, and if any is still alive the guard names them on
    stderr and calls ``os._exit`` with the session status, or 1 if that was 0.
    Worst case is about 2 x ``EXIT_GRACE_S`` (``stop_test_workers_report``).
    """

    if not lingering_worker_threads():
        return
    deadline = time.monotonic() + EXIT_GRACE_S
    try:
        stop_test_workers_report(timeout=EXIT_GRACE_S)
    except Exception:  # the join below still decides
        pass
    for thread in lingering_worker_threads():
        thread.join(max(0.0, deadline - time.monotonic()))
    stuck = lingering_worker_threads()
    if not stuck:
        return
    status = config.stash.get(_EXIT_STATUS, 1) or 1
    capman = config.pluginmanager.getplugin("capturemanager")
    if capman is not None:
        capman.stop_global_capturing()  # flush and release capture; os._exit skips its cleanup
    names = sorted(t.name for t in stuck)
    sys.stdout.flush()
    sys.stderr.write(
        f"recertia exit guard: {len(stuck)} worker thread(s) still alive past the {EXIT_GRACE_S:g}s"
        f" exit grace: {names}; os._exit({status}) so interpreter exit cannot hang on them\n"
    )
    sys.stderr.flush()
    os._exit(status)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: Any) -> None:
    """Each test starts with a closed model circuit so failures do not leak.

    It also marks where this test's app states start in the tracked list (see
    ``close_test_app_states_report``). ``tryfirst`` runs this before any fixture is set up.
    """

    track_app_states()
    item.stash[_APP_STATES_START] = len(_APP_STATES)
    from recertia.solver.resilience import default_breaker

    default_breaker().reset()


# --- App state teardown -----------------------------------------------------------------
# ``create_app`` builds an ``AppState`` that holds sqlite connections. Only the app's
# lifespan closes it, and a bare ``TestClient(app)`` never runs the lifespan, so every
# state is tracked here and closed at the end of the scope that built it:
# - built by the test body or a function-scoped fixture: after the test's own teardown
#   (``pytest_runtest_teardown``);
# - built while a class, module, package or session fixture was being set up: right
#   after that fixture's own teardown (``pytest_fixture_setup``), so the later tests in
#   its scope still get an open app.
# A state built at import time is not tracked. One built lazily during a test (by a
# wider-scoped factory, say) counts as that test's state.

_APP_STATES: list[weakref.ref[Any]] = []
_APP_STATES_START = pytest.StashKey[int]()
# One entry per fixture being set up: None for a function-scoped fixture, else the
# states built while that wider-scoped fixture was being set up.
_FIXTURE_APP_STATES: list[list[weakref.ref[Any]] | None] = []
# How long teardown waits for an app's own queue worker before closing the app.
# ``AppState.close`` would wait up to 5 s in ``embedded.stop()``. A worker that was busy
# at the pre-yield ``stop_all`` has already cost ``TEARDOWN_TIMEOUT_S`` in
# ``stop_test_workers_report``. One that became busy after it, restarted by a fixture's
# own teardown code for example, has not been waited for or reported. Either way, a
# worker still running after this bound leaves its app open, and that is reported as a
# teardown failure of the test (or scoped fixture) that owns the app. A stuck app adds at
# most 0.5 s, not 5 s, to the 60 s per-test budget.
APP_STOP_TIMEOUT_S = 0.5


def track_app_states() -> None:
    """Record every ``AppState`` built from now on (idempotent; nested sessions share it)."""

    from recertia.app.state import AppState

    original = AppState.__init__
    if getattr(original, "_tracked_for_tests", False):
        return

    @functools.wraps(original)
    def tracked_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        built = _FIXTURE_APP_STATES[-1] if _FIXTURE_APP_STATES else None
        (_APP_STATES if built is None else built).append(weakref.ref(self))

    tracked_init._tracked_for_tests = True  # type: ignore[attr-defined]
    AppState.__init__ = tracked_init  # type: ignore[method-assign]


def close_test_app_states_report(start: int) -> str | None:
    """Close the app states built since ``start`` and drop them from the list.

    ``AppState.close`` is safe to call more than once, so a state a test already
    closed (through the lifespan or ``close_app``) is closed again harmlessly. A
    nested in-process session closes only the states its own tests built. Returns
    a failure message, or None when every close returned.
    """

    refs = _APP_STATES[start:]
    del _APP_STATES[start:]
    return close_app_states_report(refs)


def close_app_states_report(refs: list[weakref.ref[Any]]) -> str | None:
    """Close each live state in ``refs``. Returns a failure message, or None.

    Each state's own queue worker is stopped first, bounded by ``APP_STOP_TIMEOUT_S``.
    If it is still running an item, the state is left open, as ``AppState.close``
    would leave it, without that method's second 5 s wait, and that is reported:
    otherwise its stores would stay open with only a log warning to show for it.
    """

    problems = []
    for ref in refs:
        state = ref()
        if state is None:
            continue
        try:
            embedded = state.embedded
            if embedded is not None and not embedded.stop(timeout=APP_STOP_TIMEOUT_S):
                problems.append("app state left open in teardown: its queue worker is still running an item")
                continue
            state.close()
        except Exception as exc:  # report every state, never skip the rest
            problems.append(f"closing an app state raised {exc!r} in teardown")
    return "; ".join(problems) or None


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef: Any, request: Any) -> Generator[None, Any, Any]:
    """Close the app states a wider-scoped fixture builds when that fixture is torn down.

    Otherwise a class, module, package or session fixture's app would count as the
    state of the first test that requested it, and be closed in that test's teardown
    under every later test in its scope. The finalizer is added before the fixture
    runs, so it runs after the fixture's own teardown code (last in, first out).
    """

    if fixturedef.scope == "function":
        _FIXTURE_APP_STATES.append(None)
    else:
        built: list[weakref.ref[Any]] = []
        _FIXTURE_APP_STATES.append(built)

        def close_fixture_app_states() -> None:
            refs = built[:]
            built.clear()
            failure = close_app_states_report(refs)
            if failure is not None:
                pytest.fail(failure, pytrace=False)

        fixturedef.addfinalizer(close_fixture_app_states)
    try:
        return (yield)
    finally:
        _FIXTURE_APP_STATES.pop()


# Teardown deadline for embedded queue workers and async pool threads. A module
# attribute so the pytester wiring test can lower it in a child session.
TEARDOWN_TIMEOUT_S = 5.0


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_teardown(item: Any, nextitem: Any) -> Generator[None, None, None]:
    """Stop every worker thread a test started (REC-029, REC-031).

    Apps built with ``create_app`` start a ``recertia-queue`` daemon thread on
    the first create or resume and nothing stopped it, so threads piled up
    across the suite. Workers are stopped *before* the ``yield``, i.e. before
    fixture finalizers, so no queue is closed under a live worker thread. A
    worker restarts on its next ``ensure_started`` (create, resume or
    ``flush``); a test that waits on the queue across a test boundary without
    one of those would stall.

    A failure is raised only *after* the ``yield``: pytest's own teardown
    (fixture finalizers, monkeypatch undo) has run, so the next test does not
    get a collateral "previous item was not torn down properly" error. An
    exception while stopping is reported the same way, never raised before
    the ``yield``: that would skip pytest's teardown and error every later test.
    If pytest's teardown itself raises, the leak message is attached to that
    exception as a note instead of being lost.

    ``trylast`` keeps this wrapper inside the capture wrapper, so output
    printed while stopping lands in the test's captured teardown section.
    """

    del nextitem
    try:
        failure = stop_test_workers_report(timeout=TEARDOWN_TIMEOUT_S)
    # pytest.fail.Exception too: pytest-timeout's signal method raises it here when the
    # 60s per-test budget runs out mid-stop, and raising before the yield cascades.
    except (Exception, pytest.fail.Exception) as exc:  # never skip pytest's own teardown
        failure = f"stopping test workers raised {exc!r} in teardown"
    start = item.stash.get(_APP_STATES_START, len(_APP_STATES))
    try:
        result = yield
    except BaseException as exc:  # a finalizer raised: keep the leak message
        closed = close_test_app_states_report(start)
        for note in (failure, closed):
            if note is not None:
                exc.add_note(note)
        raise
    # After the fixture finalizers, so a fixture that still uses its app (or closes it
    # itself) runs first; the workers were stopped above, before any store is closed.
    closed = close_test_app_states_report(start)
    failure = "; ".join(note for note in (failure, closed) if note is not None) or None
    if failure is not None:
        pytest.fail(failure, pytrace=False)
    return result


def stop_test_workers_report(timeout: float = 5.0) -> str | None:
    """Stop embedded queue workers and join async pool threads, bounded.

    Returns a failure message, or None when everything stopped. A queue worker
    still alive after ``timeout`` would leak into later tests. Async pools
    are shut down either way and their threads joined against one deadline of
    ``timeout``, so a hung run fails the test instead of hanging the
    session.
    """

    from recertia.workers.embedded import EmbeddedQueueWorker
    from recertia.workers.run_worker import AsyncRunWorker

    # Queue workers first, then pools: a live queue worker can
    # still submit a run, and a submit into a pool that shutdown_all already
    # closed silently starts a new pool whose thread outlives teardown.
    queue_stopped = EmbeddedQueueWorker.stop_all(timeout=timeout)
    pools_stopped = AsyncRunWorker.shutdown_all(timeout=timeout)
    problems = []
    if not queue_stopped:
        problems.append(f"an embedded queue worker did not stop within {timeout:g}s in teardown")
    if not pools_stopped:
        problems.append(f"an async run pool thread did not exit within {timeout:g}s in teardown")
    if not problems:
        return None
    return "; ".join(problems) + "; it would leak into later tests"


def stop_test_workers(timeout: float = 5.0) -> None:
    """``stop_test_workers_report`` that fails the current test on a leak."""

    failure = stop_test_workers_report(timeout=timeout)
    if failure is not None:
        pytest.fail(failure, pytrace=False)
