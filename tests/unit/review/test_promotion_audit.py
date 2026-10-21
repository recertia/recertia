"""REC-009: the operator-only promotion audit store fails closed and keeps full detail."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from recertia.persistence.db import open_db
from recertia.review import promotion_audit
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
    with pytest.raises(AuditSinkUnavailable, match="unset"):  # no None fallback (M1)
        open_operator_audit(root)


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


# ----- Q2 hardening (CTO-rec S1): modes, ownership, no wider-mode window -----


def test_pre_existing_root_is_tightened_to_0700(tmp_path: Path) -> None:
    root = tmp_path / "operator"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    store = OperatorAuditStore(root)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_database_is_0600_before_sqlite_opens_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []
    original = OperatorAuditStore._connect

    def _spy(self: OperatorAuditStore) -> object:
        seen.append(stat.S_IMODE(self.path.stat().st_mode))
        return original(self)

    monkeypatch.setattr(OperatorAuditStore, "_connect", _spy)
    previous = os.umask(0)
    try:
        OperatorAuditStore(tmp_path / "operator")
    finally:
        os.umask(previous)
    assert seen and set(seen) == {0o600}


def test_database_and_sidecars_are_0600_under_a_permissive_umask(tmp_path: Path) -> None:
    root = tmp_path / "operator"
    root.mkdir()
    (root / "promotion_audit.sqlite").write_bytes(b"")
    (root / "promotion_audit.sqlite").chmod(0o644)
    (root / "promotion_audit.sqlite-wal").write_bytes(b"")
    (root / "promotion_audit.sqlite-wal").chmod(0o644)
    previous = os.umask(0)
    try:
        store = OperatorAuditStore(root)
        conn = open_db(store.path)  # an open WAL connection keeps -wal and -shm on disk
        try:
            conn.execute("INSERT INTO promotion_audit VALUES ('pa-x','t','s',1,'k','{}')")
            conn.commit()
            modes = {
                p.name: stat.S_IMODE(p.stat().st_mode)
                for p in root.iterdir()
                if p.name.startswith("promotion_audit.sqlite")
            }
        finally:
            conn.close()
    finally:
        os.umask(previous)
    assert {"promotion_audit.sqlite", "promotion_audit.sqlite-wal"} <= set(modes)
    assert set(modes.values()) == {0o600}, modes
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_root_owned_by_another_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "operator"
    root.mkdir()
    owner = root.stat().st_uid
    monkeypatch.setattr(promotion_audit.os, "geteuid", lambda: owner + 1)
    with pytest.raises(AuditSinkUnavailable, match="owned by another user"):
        OperatorAuditStore(root)
    assert not (root / "promotion_audit.sqlite").exists()


def test_non_directory_root_and_symlinked_database_are_refused(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "operator-file"
    not_a_dir.write_text("x\n", encoding="utf-8")
    with pytest.raises(AuditSinkUnavailable):
        OperatorAuditStore(not_a_dir)
    root = tmp_path / "operator"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere.sqlite"
    elsewhere.write_bytes(b"")
    elsewhere.chmod(0o644)
    (root / "promotion_audit.sqlite").symlink_to(elsewhere)
    with pytest.raises(AuditSinkUnavailable, match="could not be opened") as caught:
        OperatorAuditStore(root)
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o644  # target untouched
    assert str(tmp_path) not in str(caught.value)
    assert caught.value.__context__ is None and caught.value.__cause__ is None


def test_open_and_write_errors_raise_audit_sink_unavailable_without_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_path = f"{tmp_path}/HELDOUT-x/promotion_audit.sqlite"

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise sqlite3.OperationalError(f"disk I/O error: {secret_path}")

    store = OperatorAuditStore(tmp_path / "operator")
    monkeypatch.setattr(store, "_connect", _boom)
    with pytest.raises(AuditSinkUnavailable, match="write failed") as wrote:
        store.record({"skill_id": "leak-probe", "version": 1, "refusal_kind": "golden_gate"})
    monkeypatch.setattr(promotion_audit, "open_db", _boom)
    with pytest.raises(AuditSinkUnavailable, match="could not be opened") as opened:
        OperatorAuditStore(tmp_path / "operator2")
    for caught in (wrote, opened):
        assert "HELDOUT-x" not in str(caught.value) and str(tmp_path) not in str(caught.value)
        assert caught.value.__context__ is None and caught.value.__cause__ is None
