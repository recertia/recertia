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
import errno
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


@pytest.fixture
def no_gc_cleanup(monkeypatch: pytest.MonkeyPatch) -> list[tempfile.TemporaryDirectory[str]]:
    """Keep every TemporaryDirectory alive for the test, so only explicit cleanup counts.

    A TemporaryDirectory that is never cleaned up is still removed by its finalizer once
    it is garbage-collected, which would hide a skipped cleanup in the tests that
    use this fixture. In a real run that finalizer may run late or never (a traceback or cycle keeps
    the object alive), so the dir leaks.
    """

    kept: list[tempfile.TemporaryDirectory[str]] = []

    class Kept(tempfile.TemporaryDirectory[str]):  # defined before the patch: the real class
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            kept.append(self)

    monkeypatch.setattr(tempfile, "TemporaryDirectory", Kept)
    return kept


@pytest.mark.parametrize("name", sorted(ENTRY_POINTS))
def test_probe_leaves_no_temp_dir(
    name: str, private_tmp: Path, no_gc_cleanup: list[tempfile.TemporaryDirectory[str]]
) -> None:
    with contextlib.suppress(Exception):  # a probe that raises must clean up too; its verdict
        ENTRY_POINTS[name]()  # is test_rel3_probe.py's concern, not this test's
    assert sorted(os.listdir(private_tmp)) == []


def test_probe_that_raises_leaves_no_temp_dir(
    private_tmp: Path,
    no_gc_cleanup: list[tempfile.TemporaryDirectory[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(root: Path, run: str = "r1") -> None:
        raise RuntimeError("lease backend down")

    monkeypatch.setattr(probe, "mk_lease", boom)
    with pytest.raises(RuntimeError, match="lease backend down"):
        probe.scen_token_reuse()
    assert len(no_gc_cleanup) == 1, "scen_token_reuse no longer runs in a TemporaryDirectory"
    assert sorted(os.listdir(private_tmp)) == []


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


class _FailFirstCleanup(tempfile.TemporaryDirectory[str]):
    """A TemporaryDirectory whose first ``cleanup()`` fails as a busy child would make it."""

    made: list[_FailFirstCleanup] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls = 0
        self.made.append(self)  # kept alive: only explicit cleanup counts

    def cleanup(self) -> None:
        self.calls += 1
        if self.calls == 1:
            raise OSError(errno.ENOTEMPTY, "Directory not empty (injected)", self.name)
        super().cleanup()


@pytest.mark.parametrize("raises", [False, True], ids=["returns", "raises"])
def test_removal_is_retried_once(raises: bool, private_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A first removal that fails is retried, so the dir goes and nothing is raised or noted."""

    monkeypatch.setattr(_FailFirstCleanup, "made", [])
    monkeypatch.setattr(tempfile, "TemporaryDirectory", _FailFirstCleanup)
    if raises:

        def boom(root: Path, run: str = "r1") -> None:
            raise RuntimeError("lease backend down")

        monkeypatch.setattr(probe, "mk_lease", boom)
        with pytest.raises(RuntimeError, match="lease backend down") as excinfo:
            probe.scen_token_reuse()
        assert not getattr(excinfo.value, "__notes__", None), excinfo.value.__notes__
    else:
        probe.scen_token_reuse()
    assert [t.calls for t in _FailFirstCleanup.made] == [2]
    assert sorted(os.listdir(private_tmp)) == []


def _sleeper(seconds: float) -> None:
    """Child: idle until killed, its parent is gone, or ``seconds`` pass."""

    parent = os.getppid()
    deadline = time.monotonic() + seconds
    while os.getppid() == parent and time.monotonic() < deadline:
        time.sleep(0.05)


def test_reap_spares_children_alive_before_entry(private_tmp: Path, tmp_path: Path) -> None:
    """Only children started inside the scenario are reaped, not ones alive before it."""

    elder = probe._spawn().Process(target=_sleeper, args=(30.0,), name="rec052-elder", daemon=True)
    elder.start()
    lock_dir = tmp_path / "elder-lock"
    lock_dir.mkdir()
    holders_before = set(probe._HOLDERS)
    holder = probe.subprocess_exclusive(lock_dir / ".lock")
    (elder_holder,) = probe._HOLDERS - holders_before
    try:
        probe.scen_token_reuse()  # a fresh_root scenario that starts no child of its own
        with pytest.raises(RuntimeError, match="still writing"):
            _scenario_raises_while_child_writes()  # one that does, and fails
        assert elder.is_alive(), "owned_root reaped a child that was alive before it was entered"
        assert elder_holder.poll() is None, "owned_root stopped a lock holder from before it was entered"
        assert elder_holder in probe._HOLDERS
        assert sorted(os.listdir(private_tmp)) == []
    finally:
        holder.release()
        elder.terminate()
        elder.join(timeout=5)
    assert elder_holder not in probe._HOLDERS


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


# "file:line" entries allowed to reference mkdtemp, each with its reason. Empty: every
# probe dir goes through fresh_root / owned_root / TemporaryDirectory.
MKDTEMP_ALLOWLIST: frozenset[str] = frozenset()


def _mkdtemp_references(path: Path, root: Path = REPO_ROOT) -> list[str]:
    """``file:line`` of every reference to ``mkdtemp`` in ``path``, called or not.

    Flags a Name or Attribute ``mkdtemp`` (so ``mk = tempfile.mkdtemp`` too), any
    ``from ...tempfile import mkdtemp [as x]`` or ``import *``, and the string constant
    ``"mkdtemp"`` (``getattr(tempfile, "mkdtemp")``). Comments are not code and are ignored.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = set()
    for node in ast.walk(tree):
        if (
            (isinstance(node, ast.Attribute) and node.attr == "mkdtemp")
            or (isinstance(node, ast.Name) and node.id == "mkdtemp")
            or (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").endswith("tempfile")
                and any(a.name in ("mkdtemp", "*") for a in node.names)
            )
            or (isinstance(node, ast.Constant) and node.value == "mkdtemp")
        ):
            hits.add((node.lineno, f"{path.relative_to(root)}:{node.lineno}"))
    return [hit for _, hit in sorted(hits)]


def test_probes_never_call_bare_mkdtemp() -> None:
    """Static guard: ENTRY_POINTS above is kept by hand, so a new probe could miss it."""

    files = sorted(PROBES_DIR.rglob("*.py"))
    assert files, PROBES_DIR
    hits = [hit for f in files for hit in _mkdtemp_references(f) if hit not in MKDTEMP_ALLOWLIST]
    assert hits == [], (
        f"mkdtemp in the probes leaks a temp dir nobody removes: {hits}. "
        "Fix: decorate the scenario with @fresh_root (or use `with owned_root() as root:`) "
        "from tests/probes/rel3_probe.py, or a `with tempfile.TemporaryDirectory()` block."
    )


def test_bare_mkdtemp_guard_detects_a_call(tmp_path: Path) -> None:
    """The guard itself sees the spellings a probe would use."""

    f = tmp_path / "probe.py"
    f.write_text(
        "import tempfile\nfrom tempfile import mkdtemp\na = tempfile.mkdtemp()\nb = mkdtemp(prefix='x')\n"
    )
    assert _mkdtemp_references(f, tmp_path) == ["probe.py:2", "probe.py:3", "probe.py:4"]


@pytest.mark.parametrize(
    ("source", "lines"),
    [
        pytest.param("import tempfile\nr = tempfile.mkdtemp()\n", [2], id="attribute-call"),
        pytest.param("from tempfile import mkdtemp\nr = mkdtemp()\n", [1, 2], id="from-import"),
        pytest.param("from tempfile import mkdtemp as m\nr = m()\n", [1], id="from-import-as"),
        pytest.param("from tempfile import *\nr = mkdtemp()\n", [1, 2], id="star-import"),
        pytest.param("import tempfile as t\nr = t.mkdtemp()\n", [2], id="module-alias"),
        pytest.param("import tempfile\nmk = tempfile.mkdtemp\nr = mk()\n", [2], id="assigned-alias"),
        pytest.param('import tempfile\nr = getattr(tempfile, "mkdtemp")()\n', [2], id="getattr"),
        pytest.param(
            'import importlib\nr = importlib.import_module("tempfile").mkdtemp()\n', [2], id="import_module"
        ),
    ],
)
def test_mkdtemp_guard_detects_spelling(source: str, lines: list[int], tmp_path: Path) -> None:
    f = tmp_path / "probe.py"
    f.write_text(source)
    assert _mkdtemp_references(f, tmp_path) == [f"probe.py:{n}" for n in lines]


def test_mkdtemp_guard_ignores_comments_and_other_names(tmp_path: Path) -> None:
    f = tmp_path / "probe.py"
    f.write_text(
        "import tempfile\n"
        "# never call mkdtemp( here\n"
        'x = "use mkdtemp only via fresh_root"\n'
        "with tempfile.TemporaryDirectory() as d:\n"
        "    fd, p = tempfile.mkstemp(dir=d)\n"
    )
    assert _mkdtemp_references(f, tmp_path) == []


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
