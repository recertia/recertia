"""REC-044: the REL-3 probe's own hang path runs before the pytest-timeout backstop.

The suite-wide pytest-timeout (``timeout`` in pyproject.toml) covers setup, call
and teardown. When a probe child hangs, ``_join_or_fail`` must give up first, so
the failure is the probe's own report (stack dumps, children killed) and not
"Timeout from pytest-timeout". When the backstop does pre-empt a probe test, a
hung child must not keep the session alive at exit: multiprocessing's atexit
joins every non-daemon child.

``test_probe_bounds_sit_under_the_pytest_timeout`` checks the budget at full
scale. ``test_hung_probe_child_fails_by_the_probe_not_the_backstop`` runs the
real ``test_probe_a_crash_resume_op_exactly_once`` in a child pytest with a
holder that never finishes, with the backstop and the probe's join both scaled
by ``SCALE`` so the race keeps its shape and the child stays quick. Against a
join equal to the backstop the child test fails with "Timeout from
pytest-timeout", the session then hangs at exit on the non-daemon child, and
this test fails at ``CHILD_TIMEOUT_S``.
``test_preempted_probe_test_does_not_block_session_exit`` covers the daemon half.
"""

from __future__ import annotations

import inspect
import os
import time
from pathlib import Path

import pytest

from tests.probes import rel3_probe as probe

pytest_plugins = ["pytester"]

REPO_ROOT = Path(__file__).resolve().parents[3]
# After the join gives up: _dump_hung_children sleeps 0.2s, _kill_children joins up
# to 2s after terminate and 2s after kill.
KILL_OVERHEAD_S = 5.0
# Before the join starts: fixtures, spawning children, scenario work before the join.
HEADROOM_S = 10.0
SCALE = 0.2  # the child session's backstop and probe join, as a fraction of the suite's
# backstop - join is (60s - 40s) * SCALE = 4s: at least 3x the load-sensitive part of the
# child test (setup, dump, kill, teardown: 0.15s idle, 0.6s at 4x CPU oversubscription).
MIN_CHILD_MARGIN_S = 3.0
PREEMPT_BACKSTOP_S = 2.0  # daemon check: a backstop that fires well before the probe join
PREEMPT_JOIN_S = 20.0
CHILD_TIMEOUT_S = 25.0  # a session hung at exit shows up as Pytester.TimeoutExpired
HANG_CAP_S = 45.0  # the stand-in holder exits on its own after this, or once its parent is gone


def _backstop_s(config: pytest.Config) -> float:
    return float(config.getini("timeout"))


def _join_timeout_s() -> float:
    """The join bound the scenarios actually get (``_join_or_fail``'s default)."""

    default = inspect.signature(probe._join_or_fail).parameters["timeout"].default
    return float(default)


def test_probe_bounds_sit_under_the_pytest_timeout(request: pytest.FixtureRequest) -> None:
    """The barrier breaks before the join gives up; the join and its kill path end before the backstop."""

    backstop = _backstop_s(request.config)
    join = _join_timeout_s()
    assert backstop > 0, "the suite has no pytest-timeout backstop"
    assert probe._BARRIER_TIMEOUT_S < join, (probe._BARRIER_TIMEOUT_S, join)
    assert join + KILL_OVERHEAD_S + HEADROOM_S <= backstop, (
        f"probe join {join:g}s + kill path {KILL_OVERHEAD_S:g}s + headroom {HEADROOM_S:g}s"
        f" does not end before the {backstop:g}s pytest-timeout"
    )


CHILD_CONFTEST = """
import os

import rel3_hang
from tests.probes import rel3_probe as probe


def pytest_configure(config):
    # The scenarios' own join bound, set by the parent test through REC044_CHILD_JOIN_S.
    probe._join_or_fail.__defaults__ = (float(os.environ["REC044_CHILD_JOIN_S"]),)
    probe.holder_then_crash = rel3_hang.hang
"""

HANG_MODULE = f"""
import os
import time


def hang(root, journal, run):
    # A holder that never finishes. Bounded, and gone once its parent is, so a
    # failing run of this test cannot leave it behind.
    parent = os.getppid()
    deadline = time.monotonic() + {HANG_CAP_S!r}
    while os.getppid() == parent and time.monotonic() < deadline:
        time.sleep(0.05)
"""


def _run_hung_child(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    backstop: float,
    join: float,
) -> tuple[pytest.RunResult, float]:
    """Run the real probe-A test in a child pytest whose holder never finishes."""

    parent_budget = _backstop_s(request.config)
    assert backstop + MIN_CHILD_MARGIN_S < CHILD_TIMEOUT_S < HANG_CAP_S, (backstop, CHILD_TIMEOUT_S)
    assert CHILD_TIMEOUT_S + HEADROOM_S <= parent_budget, (CHILD_TIMEOUT_S, parent_budget)
    paths = [str(REPO_ROOT / "src"), str(REPO_ROOT)]
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(paths))
    monkeypatch.setenv("REC044_CHILD_JOIN_S", repr(join))
    pytester.makeconftest(CHILD_CONFTEST)
    pytester.makepyfile(
        rel3_hang=HANG_MODULE,
        test_child="""
        from tests.unit.graph import test_rel3_probe as wiring


        def test_holder_hangs():
            wiring.test_probe_a_crash_resume_op_exactly_once()
        """,
    )
    method = request.config.getini("timeout_method") or "signal"
    started = time.monotonic()
    try:
        result = pytester.runpytest_subprocess(
            "-p",
            "tests.conftest",
            "-p",
            "no:cacheprovider",
            "-o",
            f"timeout={backstop:g}",
            "-o",
            f"timeout_method={method}",
            timeout=CHILD_TIMEOUT_S,
        )
    except pytester.TimeoutExpired:
        out = pytester.path / "stdout"  # what the killed child printed before it hung
        tail = out.read_text().splitlines()[-3:] if out.exists() else []
        pytest.fail(
            f"child session still running after {CHILD_TIMEOUT_S:g}s: a probe test pre-empted by"
            " pytest-timeout left a hung non-daemon child that blocks interpreter exit;"
            f" child output ended with {tail!r}",
            pytrace=False,
        )
    return result, time.monotonic() - started


def test_hung_probe_child_fails_by_the_probe_not_the_backstop(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """A hung holder is the probe's own "hung waiting for children" failure, and the session exits.

    Backstop and join keep the suite's real ratio (both scaled by ``SCALE``), so this
    fails if the probe join is not comfortably under the backstop.
    """

    backstop = _backstop_s(request.config) * SCALE
    join = _join_timeout_s() * SCALE
    assert backstop - join >= MIN_CHILD_MARGIN_S, (backstop, join)
    result, wall = _run_hung_child(pytester, monkeypatch, request, backstop, join)

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        ["*scen_crash_resume raised RuntimeError: scen_crash_resume hung waiting for children*"]
    )
    result.stdout.no_fnmatch_line("*from pytest-timeout*")
    assert wall < CHILD_TIMEOUT_S


def test_preempted_probe_test_does_not_block_session_exit(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """When the backstop does pre-empt a probe test, its hung child is daemonic and the session exits."""

    result, wall = _run_hung_child(pytester, monkeypatch, request, PREEMPT_BACKSTOP_S, PREEMPT_JOIN_S)

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*Timeout (>*s) from pytest-timeout*"])
    result.stdout.no_fnmatch_line("*hung waiting for children*")
    assert wall < CHILD_TIMEOUT_S
