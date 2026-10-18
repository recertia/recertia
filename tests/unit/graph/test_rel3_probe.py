"""Wire the REL-3 adversarial probe into pytest (one test per scenario).

Scenarios that need code from a later stacked PR are skipped here and enabled
on that PR. Probe assertions are not edited.
"""

from __future__ import annotations

import inspect

import pytest

from recertia.graph.lease import RunLease
from recertia.graph.store import CheckpointStore
from tests.probes import rel3_probe as probe
from tests.probes import rel3_probe_d as probe_d
from tests.probes import rel3_probe_r3 as probe_r3


def _has_persisted_leases() -> bool:
    return hasattr(RunLease, "_record_acquire")


def _has_fencing() -> bool:
    from recertia.graph import lease as lease_mod

    return hasattr(lease_mod, "require_lease_token")


def _has_lease_db_arg() -> bool:
    return "lease_db" in inspect.signature(CheckpointStore.__init__).parameters


def _has_legacy_import() -> bool:
    try:
        from recertia.persistence import import_legacy as _il
    except ImportError:
        return False
    return hasattr(_il, "import_legacy_runtime")


def _run(fn) -> list[tuple[str, bool, str]]:
    start = len(probe.RESULTS)
    fn()
    new = probe.RESULTS[start:]
    assert new, f"{fn.__name__} recorded no PASS/FAIL"
    failed = [(name, detail) for name, ok, detail in new if not ok]
    assert not failed, "; ".join(f"{name}: {detail}" for name, detail in failed)
    return new


def test_probe_a_crash_resume_op_exactly_once() -> None:
    _run(probe.scen_crash_resume)


def test_probe_a2_incarnation_never_goes_backwards() -> None:
    _run(probe.scen_incarnation_reset)


def test_probe_b_eight_racers_one_holder() -> None:
    _run(probe.scen_race)


@pytest.mark.skipif(
    not _has_persisted_leases(),
    reason="REL-3 PR #88: live lease row + lock inode (unlink must not split-brain)",
)
def test_probe_c_lock_file_unlink() -> None:
    _run(probe.scen_unlinked_lock)


@pytest.mark.skipif(
    not _has_fencing(),
    reason="REL-3 PR #89: fencing token check must be in the same transaction as the write",
)
def test_probe_e_check_then_write_atomic() -> None:
    _run(probe.scen_toctou)


def _run_d(mode: str) -> list[tuple[str, bool, str]]:
    start = len(probe_d.RESULTS)
    probe_d.scenario(mode)
    new = probe_d.RESULTS[start:]
    assert new, f"rel3_probe_d.scenario({mode!r}) recorded no PASS/FAIL"
    failed = [(name, detail) for name, ok, detail in new if not ok]
    assert not failed, "; ".join(f"{name}: {detail}" for name, detail in failed)
    return new


def test_probe_d_steal_while_held() -> None:
    _run_d("while-held")


def test_probe_d_steal_after_release() -> None:
    _run_d("after-release")


@pytest.mark.skipif(
    not _has_persisted_leases(),
    reason="REL-3 PR #88: fencing tokens must be seeded in runtime.db, not only locks/",
)
def test_probe_f_tokens_monotonic_across_restart() -> None:
    _run(probe.scen_token_reuse)


@pytest.mark.skipif(
    not _has_lease_db_arg(),
    reason="REL-3 PR #89: missing runtime.db / run_leases must fail closed",
)
def test_probe_g_missing_lease_db_rejected() -> None:
    _run(probe.scen_fail_open)


def test_probe_h_ops_rebuild_resumable_after_crash() -> None:
    _run(probe.scen_ops_rebuild_crash)


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="owned by REL-3 PR #90: open_run_persistence(migrate=) is missing on this PR",
)
def test_probe_i_no_state_lost_across_concurrent_import() -> None:
    _run(probe.scen_import_split_brain)


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="owned by REL-3 PR #90: open_run_persistence(migrate=) is missing on this PR",
)
def test_probe_j_k_import_idempotent_and_rollback() -> None:
    _run(probe.scen_import_idempotent)


def _busy_records() -> list[tuple[str, bool, str]]:
    cached = getattr(_busy_records, "_rows", None)
    if cached is not None:
        return cached
    start = len(probe.RESULTS)
    import tests.probes.rel3_probe_busy  # noqa: F401
    rows = probe.RESULTS[start:]
    _busy_records._rows = rows  # type: ignore[attr-defined]
    return rows


def _assert_named(rows: list[tuple[str, bool, str]], name: str) -> None:
    matched = [row for row in rows if row[0] == name]
    assert matched, f"probe did not record {name!r}; got {[r[0] for r in rows]}"
    failed = [(n, detail) for n, ok, detail in matched if not ok]
    assert not failed, "; ".join(f"{n}: {detail}" for n, detail in failed)


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="owned by REL-3 PR #90: open_run_persistence(migrate=) is missing on this PR",
)
def test_probe_l_legacy_visible_while_lock_busy() -> None:
    _assert_named(_busy_records(), "L legacy state visible while root lock busy")


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="owned by REL-3 PR #90: open_run_persistence(migrate=) is missing on this PR",
)
def test_probe_m_import_after_busy_keeps_state() -> None:
    _assert_named(_busy_records(), "M later import after busy-path write keeps all state")


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="owned by REL-3 PR #90: open_run_persistence(migrate=) is missing on this PR",
)
def test_probe_m2_root_openable_after_conflict() -> None:
    rows = _busy_records()
    named = [row for row in rows if row[0] == "M2 root still openable after conflict"]
    if not named:
        # M2 only records when M raises. A passing M is the success path.
        _assert_named(rows, "M later import after busy-path write keeps all state")
        return
    _assert_named(rows, "M2 root still openable after conflict")


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="owned by REL-3 PR #90: open_run_persistence(migrate=) is missing on this PR",
)
def test_probe_n_api_sees_legacy_tenant_run() -> None:
    start = len(probe.RESULTS)
    import tests.probes.rel3_probe_api_busy  # noqa: F401
    _assert_named(probe.RESULTS[start:], "N API process sees legacy tenant run after upgrade")


def _r3_records(fn) -> list[tuple[str, bool, str]]:
    cache = getattr(_r3_records, "_cache", None)
    if cache is None:
        cache = {}
        _r3_records._cache = cache  # type: ignore[attr-defined]
    key = fn.__name__
    cached = cache.get(key)
    if cached is not None:
        return cached
    start = len(probe.RESULTS)
    probe_r3.scen(fn)
    rows = probe.RESULTS[start:]
    cache[key] = rows
    return rows


def _run_r3_named(fn, name: str) -> None:
    _assert_named(_r3_records(fn), name)


def test_probe_p_fresh_run_no_lease() -> None:
    _run_r3_named(
        probe_r3.P_fresh_run_no_lease,
        "P fresh never-leased run, token-less writes allowed",
    )


def test_probe_p3_after_clean_release() -> None:
    _run_r3_named(
        probe_r3.P3_after_clean_release,
        "P3 token-less ledger append on a RELEASED run is rejected",
    )


def test_probe_p3c_never_leased_control() -> None:
    _run_r3_named(
        probe_r3.P3_after_clean_release,
        "P3c control: token-less ledger append on a never-leased run allowed",
    )


def test_probe_p4_reacquire_after_release() -> None:
    _run_r3_named(
        probe_r3.P4_reacquire_after_release,
        "P4 re-acquire after release writes with new token",
    )


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: busy open must raise typed Busy or wait until release",
)
def test_probe_o_a_typed_busy_or_block() -> None:
    _run_r3_named(
        probe_r3.O_busy_ops_exactly_once,
        "O-a open under exclusive RootLock: typed busy error or block-until-release",
    )


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: busy open must raise typed Busy or wait until release",
)
def test_probe_o_b_nothing_written_during_busy() -> None:
    _run_r3_named(
        probe_r3.O_busy_ops_exactly_once,
        "O-b nothing written during busy window",
    )


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: busy open must raise typed Busy or wait until release",
)
def test_probe_o_c_no_side_effect_or_double_run() -> None:
    _run_r3_named(
        probe_r3.O_busy_ops_exactly_once,
        "O-c no side effect during busy window, none double-run after",
    )


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: busy open must raise typed Busy or wait until release",
)
def test_probe_o_d_after_release_exact_history() -> None:
    _run_r3_named(
        probe_r3.O_busy_ops_exactly_once,
        "O-d after release fresh open = pre-existing + holder writes exactly",
    )


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: busy open must raise typed Busy or wait until release",
)
def test_probe_m3_a_typed_busy_or_block() -> None:
    _run_r3_named(
        probe_r3.M3_full_restart_conflict,
        "M3-a open under exclusive RootLock: typed busy error or block-until-release",
    )


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: busy open must raise typed Busy or wait until release",
)
def test_probe_m3_b_nothing_written_during_busy() -> None:
    _run_r3_named(
        probe_r3.M3_full_restart_conflict,
        "M3-b nothing written during busy window",
    )


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: busy open must raise typed Busy or wait until release",
)
def test_probe_m3_c_no_side_effect_or_double_run() -> None:
    _run_r3_named(
        probe_r3.M3_full_restart_conflict,
        "M3-c no side effect during busy window, none double-run after",
    )


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: busy open must raise typed Busy or wait until release",
)
def test_probe_m3_d_after_release_exact_history() -> None:
    _run_r3_named(
        probe_r3.M3_full_restart_conflict,
        "M3-d after release fresh open = pre-existing + holder writes exactly",
    )


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: busy open must raise typed Busy or wait until release",
)
def test_probe_m4_a_typed_busy_or_block() -> None:
    _run_r3_named(
        probe_r3.M4_busy_write_overwritten,
        "M4-a open under exclusive RootLock: typed busy error or block-until-release",
    )


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: busy open must raise typed Busy or wait until release",
)
def test_probe_m4_b_nothing_written_during_busy() -> None:
    _run_r3_named(
        probe_r3.M4_busy_write_overwritten,
        "M4-b nothing written during busy window",
    )


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: busy open must raise typed Busy or wait until release",
)
def test_probe_m4_c_no_side_effect_or_double_run() -> None:
    _run_r3_named(
        probe_r3.M4_busy_write_overwritten,
        "M4-c no side effect during busy window, none double-run after",
    )


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: busy open must raise typed Busy or wait until release",
)
def test_probe_m4_d_after_release_exact_history() -> None:
    _run_r3_named(
        probe_r3.M4_busy_write_overwritten,
        "M4-d after release fresh open = pre-existing + holder writes exactly",
    )


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: migrate=off after import must see post-import writes",
)
def test_probe_q_off_after_import_sees_new_writes() -> None:
    _run_r3_named(
        probe_r3.Q_off_after_import_sees_new_writes,
        "Q migrate=off (rollback) sees writes made after import",
    )


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: migrate=off after import must see post-import writes",
)
def test_probe_q2_roll_forward_after_off_writes() -> None:
    _run_r3_named(
        probe_r3.Q_off_after_import_sees_new_writes,
        "Q2 roll forward again (auto) after off-mode writes",
    )


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: import completion is a runtime.db marker, not leftover files",
)
def test_probe_r_legacy_not_pending_after_import() -> None:
    _run_r3_named(
        probe_r3.R_reimport_every_open,
        "R legacy not 'pending' after a completed import",
    )


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: backup/restore must refuse while the API holds api.lock",
)
def test_probe_s_backup_refused_while_api_up() -> None:
    _run_r3_named(
        probe_r3.S_backup_refused_while_api_up,
        "S backup/restore still refused while API process is up",
    )
