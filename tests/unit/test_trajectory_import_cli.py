"""Phase 0: recertia trajectory import rejects incomplete provenance and never promotes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from recertia.cli.main import app
from recertia.memory.episodic import EpisodicStore
from recertia.trajectory.import_store import ImportRejected, ingest_trajectory

runner = CliRunner()


def _valid_payload(**overrides: object) -> dict:
    base: dict = {
        "import_id": "imp-001",
        "source": "synthetic",
        "source_ref": "fixture://repro",
        "captured_at": datetime(2026, 8, 23, tzinfo=timezone.utc).isoformat(),
        "environment": {"os": "linux", "tools": ["browser"], "network_policy": "none"},
        "steps": [{"seq": 0, "action": "open", "target": "/login", "observed": "form"}],
        "outcome": "solved",
        "criteria_snapshot": [],
        "provenance": {"source": "synthetic", "source_ref": "fixture://repro"},
        "artifacts": [],
        "reexecutable": False,
        "require_auditor_reverify": True,
    }
    base.update(overrides)
    return base


def test_ingest_writes_episodic_and_does_not_promote(tmp_path: Path) -> None:
    result = ingest_trajectory(_valid_payload(), runs_root=tmp_path, tenant_id="default")
    assert result.promoted is False
    assert result.may_promote is False
    assert result.promote_reason == "not_reexecutable"
    assert (tmp_path / "runs" / "default" / "imports" / "imp-001.json").exists()
    rec = EpisodicStore(tmp_path / "runs" / "default" / "episodic").get_by_case_id(
        "import-imp-001"
    )
    assert rec is not None
    assert rec.approach == "external:synthetic"
    assert rec.outcome == "solved"


def test_reexecutable_still_does_not_promote(tmp_path: Path) -> None:
    payload = _valid_payload(import_id="imp-002", reexecutable=True)
    result = ingest_trajectory(payload, runs_root=tmp_path)
    assert result.promoted is False
    assert result.may_promote is False
    assert result.promote_reason == "missing_criteria_snapshot"


def test_duplicate_import_rejected(tmp_path: Path) -> None:
    ingest_trajectory(_valid_payload(), runs_root=tmp_path)
    with pytest.raises(ImportRejected, match="already exists"):
        ingest_trajectory(_valid_payload(), runs_root=tmp_path)


def test_import_id_path_escape_rejected(tmp_path: Path) -> None:
    with pytest.raises(ImportRejected, match="import_id"):
        ingest_trajectory(_valid_payload(import_id="../etc/passwd"), runs_root=tmp_path)
    with pytest.raises(ImportRejected, match="import_id"):
        ingest_trajectory(_valid_payload(import_id="imp/001"), runs_root=tmp_path)


def test_tenants_do_not_share_import_files(tmp_path: Path) -> None:
    ingest_trajectory(_valid_payload(import_id="imp-t"), runs_root=tmp_path, tenant_id="a")
    ingest_trajectory(_valid_payload(import_id="imp-t"), runs_root=tmp_path, tenant_id="b")
    assert (tmp_path / "runs" / "a" / "imports" / "imp-t.json").exists()
    assert (tmp_path / "runs" / "b" / "imports" / "imp-t.json").exists()


def test_cli_import_rejects_and_accepts(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    denied = runner.invoke(app, ["trajectory", "import", str(bad), "--runs-root", str(tmp_path)])
    assert denied.exit_code == 1
    combined = (denied.output + denied.stdout + denied.stderr).lower()
    assert "rejected" in combined or "ok" in combined

    empty_env = tmp_path / "empty-env.json"
    empty_env.write_text(
        json.dumps(_valid_payload(import_id="imp-empty", environment={"network_policy": "none"})),
        encoding="utf-8",
    )
    env_denied = runner.invoke(
        app, ["trajectory", "import", str(empty_env), "--runs-root", str(tmp_path)]
    )
    assert env_denied.exit_code == 1

    good = tmp_path / "good.json"
    good.write_text(json.dumps(_valid_payload(import_id="imp-cli")), encoding="utf-8")
    ok = runner.invoke(app, ["trajectory", "import", str(good), "--runs-root", str(tmp_path)])
    assert ok.exit_code == 0, ok.output
    body = json.loads(ok.output)
    assert body["ok"] is True
    assert body["promoted"] is False
    assert body["import_id"] == "imp-cli"
