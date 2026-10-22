"""REC-017: ``faulthandler_timeout`` backs up the pytest-timeout backstop.

``timeout_method = "signal"`` runs pytest-timeout's handler on the main thread,
and CPython runs a Python signal handler only between bytecodes. A main thread
blocked inside C (the REC-017 child sat in ``pthread_mutex_lock`` under
``sqlite3.connect``) never reaches it, so pytest-timeout cannot fire or print
anything until the C call returns, which may be never. ``faulthandler_timeout``
arms a native watchdog thread for each test; it dumps every thread's stack when
the test runs past the limit, whatever the main thread is doing. It only dumps
(``faulthandler_exit_on_timeout`` stays off); failing the test is still
pytest-timeout's job, and the CI job timeout is the last resort.

``test_faulthandler_timeout_is_set_within_the_backstop`` checks the live config.
``test_c_level_block_is_dumped_by_faulthandler_not_pytest_timeout`` runs a child
pytest whose test blocks in ``pthread_mutex_timedlock`` past both limits, scaled
down, and checks that the watchdog's dump arrives while pytest-timeout is still
waiting for the C call to return.
"""

from __future__ import annotations

import sys
import time

import pytest

pytest_plugins = ["pytester"]

CHILD_LIMIT_S = 1  # the child's timeout and faulthandler_timeout
HOLD_S = 4  # how long the child's main thread stays in C, past CHILD_LIMIT_S
CHILD_TIMEOUT_S = 25.0  # HOLD_S plus interpreter start-up, with room under the 60s backstop

CHILD_TEST = f"""
import ctypes
import time


class _Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


def test_blocks_in_c():
    libc = ctypes.CDLL(None, use_errno=True)
    mutex = ctypes.create_string_buffer(64)  # zeroed: a default glibc pthread mutex
    assert libc.pthread_mutex_lock(mutex) == 0
    deadline = _Timespec(int(time.time()) + {HOLD_S}, 0)
    # Already held by this thread, so this waits in C until the deadline.
    # Signals do not interrupt it, so no Python handler runs meanwhile.
    rc = libc.pthread_mutex_timedlock(mutex, ctypes.byref(deadline))
    print("timedlock returned", rc)
"""


def test_faulthandler_timeout_is_set_within_the_backstop(request: pytest.FixtureRequest) -> None:
    """The suite arms faulthandler's watchdog, no later than the pytest-timeout backstop."""

    backstop = float(request.config.getini("timeout"))
    watchdog = float(request.config.getini("faulthandler_timeout") or 0.0)
    assert backstop > 0, "the suite has no pytest-timeout backstop"
    assert 0 < watchdog <= backstop, (watchdog, backstop)
    assert request.config.getini("faulthandler_exit_on_timeout") is False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="glibc pthread mutex via ctypes")
@pytest.mark.parametrize("watchdog", [True, False], ids=["faulthandler_timeout", "no_faulthandler_timeout"])
def test_c_level_block_is_dumped_by_faulthandler_not_pytest_timeout(
    pytester: pytest.Pytester, request: pytest.FixtureRequest, watchdog: bool
) -> None:
    """While the main thread is in C, only the watchdog reports; pytest-timeout fails the test afterwards.

    Without ``faulthandler_timeout`` nothing is printed until the C call returns.
    """

    assert CHILD_TIMEOUT_S < float(request.config.getini("timeout"))
    pytester.makepyfile(test_child=CHILD_TEST)
    started = time.monotonic()
    result = pytester.runpytest_subprocess(
        "-p",
        "no:cacheprovider",
        "-o",
        f"timeout={CHILD_LIMIT_S}",
        "-o",
        "timeout_method=signal",
        "-o",
        f"faulthandler_timeout={CHILD_LIMIT_S if watchdog else 0}",
        timeout=CHILD_TIMEOUT_S,
    )
    wall = time.monotonic() - started

    # pytest-timeout's SIGALRM handler only ran once the C call had returned.
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines([f"*Timeout (>{CHILD_LIMIT_S:.1f}s) from pytest-timeout*"])
    assert wall >= HOLD_S - 1, wall
    if watchdog:
        # faulthandler's watchdog dumped the stack while the main thread was still in C.
        result.stderr.fnmatch_lines([f"Timeout (0:00:0{CHILD_LIMIT_S})!", "*in test_blocks_in_c*"])
    else:
        result.stderr.no_fnmatch_line("Timeout (0:00:*)!")
