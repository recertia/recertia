"""REC-047: the REL-3 probes remove the temp dirs they create.

Every probe entry point the suite runs is called with ``tempfile`` pointed at an empty
private directory; afterwards that directory must still be empty, whether the probe
recorded PASS or FAIL or raised. The probes' verdicts are checked in test_rel3_probe.py.

REC-052 adds: cleanup on a ``BaseException`` that is not an ``Exception`` (KeyboardInterrupt,
pytest-timeout's failure; the #112 defect class), children reaped before the dir is
removed so a still-writing child cannot turn the scenario's error into
``OSError: Directory not empty``, a static guard against bare ``mkdtemp(`` in the probes
(``ENTRY_POINTS`` is maintained by hand), and no unclosed pipes or files from the probes.
"""

from __future__ import annotations

import ast
import contextlib
import functools
import gc
import os
import runpy
import tempfile
import time
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.probes import rel3_probe as probe
from tests.probes import rel3_probe_d as probe_d
from tests.probes import rel3_probe_r3 as probe_r3
from tests.probes import rel3_probe_r4 as probe_r4

pytest_plugins = ["pytester"]

REPO_ROOT = Path(__file__).resolve().parents[3]


def _module(name: str) -> Callable[[], object]:
    """Run a probe that does its work at import time, afresh on every call."""
    return functools.partial(runpy.run_path, str(Path(probe.__file__).with_name(f"{name}.py")), run_name=name)


ENTRY_POINTS: dict[str, Callable[[], object]] = {
    **{
        f.__name__: f
        for f in (
            probe.scen_crash_resume,
            probe.scen_incarnation_reset,
            probe.scen_race,
            probe.scen_unlinked_lock,
            probe.scen_toctou,
            probe.scen_token_reuse,
            probe.scen_fail_open,
            probe.scen_ops_rebuild_crash,
            probe.scen_import_split_brain,
            probe.scen_import_idempotent,
        )
    },
    "d_while_held": functools.partial(probe_d.scenario, "while-held"),
    "d_after_release": functools.partial(probe_d.scenario, "after-release"),
    **{
        f"r3_{f.__name__}": f
        for f in (
            probe_r3.P_fresh_run_no_lease,
            probe_r3.P3_after_clean_release,
            probe_r3.P4_reacquire_after_release,
            probe_r3.O_busy_ops_exactly_once,
            probe_r3.M3_full_restart_conflict,
            probe_r3.M4_busy_write_overwritten,
            probe_r3.Q_off_after_import_sees_new_writes,
            probe_r3.R_reimport_every_open,
            probe_r3.S_backup_refused_while_api_up,
        )
    },
    **{
        f"r4_{f.__name__}": f
        for f in (
            probe_r4.T1_same_thread_ops_double_run,
            probe_r4.T2_same_thread_ghost_write,
            probe_r4.T3_stale_holder_identity,
            probe_r4.T3b_crash_leaves_identity,
            probe_r4.T4_asyncio_same_thread,
            probe_r4.T5_threadpool_reuse,
        )
    },
    "rel3_probe_api_busy": _module("rel3_probe_api_busy"),
    "rel3_probe_busy": _module("rel3_probe_busy"),
}


@pytest.fixture
def private_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty directory that ``tempfile`` (and any child process) uses as its temp dir."""

    scratch = tmp_path / "tmpdir"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    return scratch


@pytest.mark.parametrize("name", sorted(ENTRY_POINTS))
def test_probe_leaves_no_temp_dir(name: str, private_tmp: Path) -> None:
    with contextlib.suppress(Exception):  # a probe that raises must clean up too; its verdict
        ENTRY_POINTS[name]()  # is test_rel3_probe.py's concern, not this test's
    assert sorted(os.listdir(private_tmp)) == []


def test_probe_that_raises_leaves_no_temp_dir(private_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(root: Path, run: str = "r1") -> None:
        raise RuntimeError("lease backend down")

    monkeypatch.setattr(probe, "mk_lease", boom)
    with pytest.raises(RuntimeError, match="lease backend down"):
        probe.scen_token_reuse()
    assert sorted(os.listdir(private_tmp)) == []


@pytest.fixture
def no_gc_cleanup(monkeypatch: pytest.MonkeyPatch) -> list[tempfile.TemporaryDirectory[str]]:
    """Keep every TemporaryDirectory alive for the test, so only explicit cleanup counts.

    A TemporaryDirectory that is never cleaned up is still removed by its finalizer once
    it is garbage-collected, which would hide a skipped cleanup in a test like the one
    below. In a real run that finalizer may run late or never (a traceback or cycle keeps
    the object alive), so the dir leaks.
    """

    kept: list[tempfile.TemporaryDirectory[str]] = []

    class Kept(tempfile.TemporaryDirectory[str]):  # defined before the patch: the real class
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            kept.append(self)

    monkeypatch.setattr(tempfile, "TemporaryDirectory", Kept)
    return kept


@pytest.mark.parametrize(
    "interrupt",
    [
        pytest.param(lambda: KeyboardInterrupt(), id="KeyboardInterrupt"),
        # What pytest-timeout's signal method raises: Failed, an OutcomeException, so a
        # BaseException and not an Exception.
        pytest.param(lambda: pytest.fail.Exception("Timeout (>3.0s) from pytest-timeout"), id="timeout"),
    ],
)
def test_probe_interrupted_by_base_exception_leaves_no_temp_dir(
    interrupt: Callable[[], BaseException],
    private_tmp: Path,
    no_gc_cleanup: list[tempfile.TemporaryDirectory[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup must not be an ``except Exception`` path (#112): it runs on any BaseException."""

    exc = interrupt()
    assert not isinstance(exc, Exception)

    def boom(root: Path, run: str = "r1") -> None:
        raise exc

    monkeypatch.setattr(probe, "mk_lease", boom)
    with pytest.raises(type(exc)) as excinfo:
        probe.scen_token_reuse()
    assert excinfo.value is exc
    assert len(no_gc_cleanup) == 1, "scen_token_reuse no longer runs in a TemporaryDirectory"
    assert sorted(os.listdir(private_tmp)) == []


WRITER_RING = 200


def _writer(root: str, ready: Any) -> None:
    """Child: keep creating entries under ``root`` (like a sqlite -journal/-wal writer).

    Stops when ``root`` is gone, when its parent is gone, or after 20s.
    """

    parent = os.getppid()
    sub = Path(root) / "w"
    sub.mkdir()
    ready.set()
    deadline = time.monotonic() + 20
    i = 0
    while os.getppid() == parent and time.monotonic() < deadline:
        try:
            (sub / f"f{i}").touch()
            with contextlib.suppress(FileNotFoundError):
                (sub / f"f{i - WRITER_RING}").unlink()
        except FileNotFoundError:
            try:
                sub.mkdir()  # rmtree took the subdir; recreate it if root is still there
            except FileNotFoundError:
                return
        i += 1


@probe.fresh_root
def _scenario_raises_while_child_writes(root: Path) -> None:
    ctx = probe._spawn()
    ready = ctx.Event()
    ctx.Process(target=_writer, args=(str(root), ready), name="rec052-writer", daemon=True).start()
    assert ready.wait(20), "writer child did not start"
    time.sleep(0.2)
    raise RuntimeError("scenario failed while its child was still writing")


def test_probe_reaps_children_before_removing_its_dir(private_tmp: Path) -> None:
    """A child still writing into the root is reaped first: no leak, the real error survives.

    Without the reap, rmtree races the child and fails with ``OSError: Directory not
    empty``, which replaces the scenario's own error and leaves the dir behind.
    """

    for _ in range(3):
        with pytest.raises(RuntimeError, match="still writing") as excinfo:
            _scenario_raises_while_child_writes()
        assert not getattr(excinfo.value, "__notes__", None), excinfo.value.__notes__
        assert sorted(os.listdir(private_tmp)) == []
        assert [p for p in probe.mp.active_children() if p.name == "rec052-writer"] == []


CHILD_BACKSTOP_S = 2.0
CHILD_TIMEOUT_S = 30.0

TIMEOUT_CHILD = """
import time

from tests.probes import rel3_probe as probe
from tests.unit.graph.test_rel3_probe_tmp_cleanup import _writer


@probe.fresh_root
def scenario(root):
    ctx = probe._spawn()
    ready = ctx.Event()
    p = ctx.Process(target=_writer, args=(str(root), ready), name="rec052-writer", daemon=True)
    p.start()
    assert ready.wait(20)
    probe._join_or_fail("rec052", [p], timeout=20)  # pytest-timeout fires during this join


def test_times_out_mid_join():
    scenario()
"""


def test_timeout_mid_join_reaps_children_and_keeps_the_timeout(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real pytest-timeout (signal method) firing inside ``_join_or_fail``'s join.

    The child pytest must report the timeout itself (not an OSError from rmtree) and
    leave nothing in its TMPDIR.
    """

    child_tmp = tmp_path / "child-tmpdir"
    child_tmp.mkdir()
    paths = [str(REPO_ROOT / "src"), str(REPO_ROOT)]
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(paths))
    monkeypatch.setenv("TMPDIR", str(child_tmp))
    pytester.makepyfile(test_child=TIMEOUT_CHILD)
    result = pytester.runpytest_subprocess(
        "-p",
        "tests.conftest",
        "-p",
        "no:cacheprovider",
        f"--basetemp={tmp_path / 'child-basetemp'}",
        "-o",
        f"timeout={CHILD_BACKSTOP_S:g}",
        "-o",
        "timeout_method=signal",
        timeout=CHILD_TIMEOUT_S,
    )
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*Timeout (>*s) from pytest-timeout*"])
    result.stdout.no_fnmatch_line("*Directory not empty*")
    result.stdout.no_fnmatch_line("*fresh_root: could not remove*")
    assert sorted(os.listdir(child_tmp)) == []


PROBES_DIR = Path(probe.__file__).parent


def _bare_mkdtemp_calls(path: Path, root: Path = REPO_ROOT) -> list[str]:
    """``file:line`` of every ``mkdtemp(...)`` / ``<anything>.mkdtemp(...)`` call in ``path``."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "mkdtemp":
                hits.append(f"{path.relative_to(root)}:{node.lineno}")
    return hits


def test_probes_never_call_bare_mkdtemp() -> None:
    """Static guard: ENTRY_POINTS above is kept by hand, so a new probe could miss it."""

    files = sorted(PROBES_DIR.rglob("*.py"))
    assert files, PROBES_DIR
    hits = [hit for f in files for hit in _bare_mkdtemp_calls(f)]
    assert hits == [], (
        f"bare mkdtemp() in the probes leaks a temp dir nobody removes: {hits}. "
        "Fix: decorate the scenario with @fresh_root (or use `with owned_root() as root:`) "
        "from tests/probes/rel3_probe.py, or a `with tempfile.TemporaryDirectory()` block."
    )


def test_bare_mkdtemp_guard_detects_a_call(tmp_path: Path) -> None:
    """The guard itself sees the spellings a probe would use."""

    f = tmp_path / "probe.py"
    f.write_text(
        "import tempfile\nfrom tempfile import mkdtemp\na = tempfile.mkdtemp()\nb = mkdtemp(prefix='x')\n"
    )
    assert _bare_mkdtemp_calls(f, tmp_path) == ["probe.py:3", "probe.py:4"]


RESOURCE_PROBES = {
    "scen_import_split_brain": probe.scen_import_split_brain,  # subprocess_exclusive pipes
    "rel3_probe_busy": ENTRY_POINTS["rel3_probe_busy"],  # subprocess_exclusive pipes
    "r3_M4_busy_write_overwritten": probe_r3.M4_busy_write_overwritten,  # .effects reads
}


@pytest.mark.parametrize("name", sorted(RESOURCE_PROBES))
def test_probe_closes_its_pipes_and_files(name: str, private_tmp: Path) -> None:
    """REC-052: no ResourceWarning for the holder's pipes or the busy .effects journal."""

    gc.collect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        with contextlib.suppress(Exception):
            RESOURCE_PROBES[name]()
        gc.collect()
    leaks = [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)]
    assert leaks == []
