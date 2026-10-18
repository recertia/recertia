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
