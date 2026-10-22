"""REC-047: the REL-3 probes remove the temp dirs they create.

Every probe entry point the suite runs is called with ``tempfile`` pointed at an empty
private directory; afterwards that directory must still be empty, whether the probe
recorded PASS or FAIL or raised. The probes' verdicts are checked in test_rel3_probe.py.
"""

from __future__ import annotations

import contextlib
import functools
import os
import runpy
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.probes import rel3_probe as probe
from tests.probes import rel3_probe_d as probe_d
from tests.probes import rel3_probe_r3 as probe_r3
from tests.probes import rel3_probe_r4 as probe_r4


def _module(name: str) -> Callable[[], object]:
    """Run a probe that does its work at import time, afresh on every call."""
    return functools.partial(runpy.run_path, str(Path(probe.__file__).with_name(f"{name}.py")), run_name=name)


ENTRY_POINTS: dict[str, Callable[[], object]] = {
    **{f.__name__: f for f in (
        probe.scen_crash_resume, probe.scen_incarnation_reset, probe.scen_race,
        probe.scen_unlinked_lock, probe.scen_toctou, probe.scen_token_reuse, probe.scen_fail_open,
        probe.scen_ops_rebuild_crash, probe.scen_import_split_brain, probe.scen_import_idempotent,
    )},
    "d_while_held": functools.partial(probe_d.scenario, "while-held"),
    "d_after_release": functools.partial(probe_d.scenario, "after-release"),
    **{f"r3_{f.__name__}": f for f in (
        probe_r3.P_fresh_run_no_lease, probe_r3.P3_after_clean_release,
        probe_r3.P4_reacquire_after_release, probe_r3.O_busy_ops_exactly_once,
        probe_r3.M3_full_restart_conflict, probe_r3.M4_busy_write_overwritten,
        probe_r3.Q_off_after_import_sees_new_writes, probe_r3.R_reimport_every_open,
        probe_r3.S_backup_refused_while_api_up,
    )},
    **{f"r4_{f.__name__}": f for f in (
        probe_r4.T1_same_thread_ops_double_run, probe_r4.T2_same_thread_ghost_write,
        probe_r4.T3_stale_holder_identity, probe_r4.T3b_crash_leaves_identity,
        probe_r4.T4_asyncio_same_thread, probe_r4.T5_threadpool_reuse,
    )},
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


def test_probe_that_raises_leaves_no_temp_dir(
    private_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(root: Path, run: str = "r1") -> None:
        raise RuntimeError("lease backend down")

    monkeypatch.setattr(probe, "mk_lease", boom)
    with pytest.raises(RuntimeError, match="lease backend down"):
        probe.scen_token_reuse()
    assert sorted(os.listdir(private_tmp)) == []
