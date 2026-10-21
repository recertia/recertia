"""REC-009: the operator-only promotion audit store fails closed and keeps full detail."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from recertia.review.promotion_audit import (
    AuditSinkUnavailable,
    OperatorAuditStore,
    open_operator_audit,
)
from recertia.settings import KNOWN_ENV_KEYS, load_settings


@pytest.mark.parametrize("root", [None, "", "   "])
def test_unset_root_fails_closed(root: str | None) -> None:
    with pytest.raises(AuditSinkUnavailable, match="unset"):
        OperatorAuditStore(root)
    assert open_operator_audit(root) is None


def test_root_inside_a_forbidden_root_or_checkout_is_refused(tmp_path: Path) -> None:
    api_root = tmp_path / "api-root"
    runs_root = api_root / "runs" / "t1"
    runs_root.mkdir(parents=True)
    for root in (api_root, runs_root, runs_root / "audit"):
        with pytest.raises(AuditSinkUnavailable, match="outside"):
            OperatorAuditStore(root, forbidden_roots=[api_root])
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    with pytest.raises(AuditSinkUnavailable, match="git checkout"):
        OperatorAuditStore(checkout / "audit")
    link = tmp_path / "sneaky"
    link.symlink_to(runs_root, target_is_directory=True)
    with pytest.raises(AuditSinkUnavailable):
        OperatorAuditStore(link, forbidden_roots=[api_root])


def test_record_and_read_round_trip_with_opaque_ref(tmp_path: Path) -> None:
    store = OperatorAuditStore(tmp_path / "operator", forbidden_roots=[tmp_path / "api"])
    ref = store.record(
        {
            "skill_id": "leak-probe",
            "version": 2,
            "refusal_kind": "golden_gate",
            "failing_fixtures": ["HELDOUT-x"],
            "results": [{"golden_path": "/synthetic/HELDOUT-x", "passed": False}],
            "detail": "synthetic detail",
        }
    )
    assert ref.startswith("pa-") and "HELDOUT" not in ref and "leak" not in ref
    record = store.read(ref)
    assert record is not None
    assert record["failing_fixtures"] == ["HELDOUT-x"]
    assert record["results"][0]["golden_path"] == "/synthetic/HELDOUT-x"
    assert store.read("pa-unknown") is None
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    with pytest.raises(ValueError):
        store.record({"skill_id": "x", "surprise": 1})


def test_settings_reads_operator_audit_root() -> None:
    assert "RECERTIA_OPERATOR_AUDIT_ROOT" in KNOWN_ENV_KEYS
    assert load_settings({"RECERTIA_OPERATOR_AUDIT_ROOT": " /srv/op "}).operator_audit_root == (
        "/srv/op"
    )
    assert load_settings({}).operator_audit_root == ""
