"""Wire the REL-3 adversarial probe into pytest (one test per scenario).

Scenarios that need code from a later stacked PR are skipped here and enabled
on that PR. Probe assertions are not edited.
"""

from __future__ import annotations

import inspect
import tempfile
from pathlib import Path

import pytest

from recertia.graph.lease import RunLease
from recertia.graph.persistence import open_run_persistence
from recertia.graph.store import CheckpointStore
from recertia.persistence.root_lock import RootLock
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
    try:
        fn()
    except Exception as exc:
        pytest.fail(f"{fn.__name__} raised {type(exc).__name__}: {exc}")
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
    reason="REL-3 PR #90: import_legacy must not fall back to files while the root lock is busy",
)
def test_probe_i_no_state_lost_across_concurrent_import() -> None:
    _run(probe.scen_import_split_brain)


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: import idempotence, schema versioning, migrate=off rollback",
)
def test_probe_j_k_import_idempotent_and_rollback() -> None:
    _run(probe.scen_import_idempotent)


def _busy_records() -> list[tuple[str, bool, str]]:
    cached = getattr(_busy_records, "_rows", None)
    if cached is not None:
        return cached
    start = len(probe.RESULTS)
    try:
        import tests.probes.rel3_probe_busy  # noqa: F401
    except Exception as exc:
        if not any(row[0].startswith("L ") for row in probe.RESULTS[start:]):
            probe.record(
                "L legacy state visible while root lock busy",
                False,
                f"probe error {type(exc).__name__}: {exc}",
            )
        if not any(
            row[0].startswith("M later") for row in probe.RESULTS[start:]
        ):
            probe.record(
                "M later import after busy-path write keeps all state",
                False,
                f"probe error {type(exc).__name__}: {exc}",
            )
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
    reason="REL-3 PR #90: lock-busy import must keep leftover state visible",
)
def test_probe_l_legacy_visible_while_lock_busy() -> None:
    _assert_named(_busy_records(), "L legacy state visible while root lock busy")


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: lock-busy import must keep leftover state visible",
)
def test_probe_m_import_after_busy_keeps_state() -> None:
    _assert_named(_busy_records(), "M later import after busy-path write keeps all state")


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: lock-busy import must keep leftover state visible",
)
def test_probe_m2_root_openable_after_conflict() -> None:
    """M2 is independent of the busy.py except-path (which only records when M raises)."""

    root = Path(tempfile.mkdtemp())
    p0 = open_run_persistence(root, migrate="off")
    try:
        for seq in (1, 2, 3):
            p0.checkpoints.save(
                "legacy-run", seq, f"n{seq}", None, probe.state("legacy-run")
            )
    finally:
        p0.close()
    lock = RootLock(root / ".lock")
    lock.try_exclusive()
    try:
        try:
            open_run_persistence(root).close()
        except Exception:
            pass
    finally:
        lock.release()
    start = len(probe.RESULTS)
    try:
        opened = open_run_persistence(root)
        opened.close()
        again = "opens"
    except Exception as exc:
        again = f"still raises {type(exc).__name__}"
    probe.record("M2 root still openable after conflict", again == "opens", again)
    _assert_named(probe.RESULTS[start:], "M2 root still openable after conflict")


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: API must not hold root/.lock across import",
)
def test_probe_n_api_sees_legacy_tenant_run() -> None:
    start = len(probe.RESULTS)
    import tests.probes.rel3_probe_api_busy  # noqa: F401
    _assert_named(probe.RESULTS[start:], "N API process sees legacy tenant run after upgrade")


def _run_r3(fn) -> list[tuple[str, bool, str]]:
    start = len(probe.RESULTS)
    probe_r3.scen(fn)
    new = probe.RESULTS[start:]
    assert new, f"{fn.__name__} recorded no PASS/FAIL"
    failed = [(name, detail) for name, ok, detail in new if not ok]
    assert not failed, "; ".join(f"{name}: {detail}" for name, detail in failed)
    return new


def _run_r3_named(fn, name: str) -> None:
    start = len(probe.RESULTS)
    probe_r3.scen(fn)
    new = probe.RESULTS[start:]
    _assert_named(new, name)


def test_probe_p_fresh_run_no_lease() -> None:
    _run_r3(probe_r3.P_fresh_run_no_lease)


def test_probe_p3_after_clean_release() -> None:
    _run_r3(probe_r3.P3_after_clean_release)


def test_probe_p4_reacquire_after_release() -> None:
    _run_r3(probe_r3.P4_reacquire_after_release)


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: busy open must not re-run a finished legacy op",
)
def test_probe_o_busy_ops_exactly_once() -> None:
    _run_r3(probe_r3.O_busy_ops_exactly_once)


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: busy-path rewrite must not wedge the root",
)
def test_probe_m3_full_restart_conflict() -> None:
    _run_r3(probe_r3.M3_full_restart_conflict)


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: import must not overwrite busy-path writes",
)
def test_probe_m4_busy_write_overwritten() -> None:
    _run_r3(probe_r3.M4_busy_write_overwritten)


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
    _run_r3(probe_r3.R_reimport_every_open)


@pytest.mark.skipif(
    not _has_legacy_import(),
    reason="REL-3 PR #90: backup/restore must refuse while the API holds api.lock",
)
def test_probe_s_backup_refused_while_api_up() -> None:
    _run_r3(probe_r3.S_backup_refused_while_api_up)
