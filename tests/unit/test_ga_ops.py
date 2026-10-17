"""Operator-GA scaffolding: backup/restore, tabletop walker, live canary."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from recertia.cli.main import app
from recertia.evals.canary import LiveCanaryError, run_judge_canary, run_live_verifier_canary
from recertia.ops.backup import BackupError, backup_tree, restore_tree
from recertia.solver.model import StubModelClient

runner = CliRunner()


def test_backup_round_trip_and_refuses_escape(tmp_path: Path) -> None:
    root = tmp_path / "recertia"
    (root / "runs").mkdir(parents=True)
    (root / "runs" / "note.txt").write_text("keep-me\n", encoding="utf-8")
    archive = tmp_path / "backups" / "recertia.tar.gz"
    backup_tree(root, archive)
    dest = tmp_path / "restored"
    restore_tree(archive, dest)
    assert (dest / "runs" / "note.txt").read_text(encoding="utf-8") == "keep-me\n"

    nested = root / "inside.tar.gz"
    try:
        backup_tree(root, nested)
        raise AssertionError("expected BackupError for archive inside root")
    except BackupError:
        pass

    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        payload = b"nope"
        info = tarfile.TarInfo(name="../escaped.txt")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    dest_escape = tmp_path / "restore-escape"
    dest_escape.mkdir()
    try:
        restore_tree(evil, dest_escape)
        raise AssertionError("expected BackupError for path-escaping member")
    except BackupError:
        pass
    assert not (tmp_path / "escaped.txt").exists()


def test_online_sqlite_backup_includes_committed_wal(tmp_path: Path) -> None:
    from recertia.persistence.db import open_db

    root = tmp_path / "recertia"
    root.mkdir()
    db_path = root / "checkpoints.db"
    live = open_db(db_path)
    live.execute("CREATE TABLE hops (id INTEGER PRIMARY KEY, note TEXT)")
    live.execute("INSERT INTO hops (note) VALUES ('committed')")
    live.commit()
    archive = tmp_path / "backups" / "recertia.tar.gz"
    backup_tree(root, archive)
    live.execute("INSERT INTO hops (note) VALUES ('after')")
    live.commit()
    live.close()

    dest = tmp_path / "restored"
    restore_tree(archive, dest)
    restored = open_db(dest / "checkpoints.db")
    try:
        notes = [row[0] for row in restored.execute("SELECT note FROM hops ORDER BY id")]
    finally:
        restored.close()
    assert notes == ["committed"]
    assert not (dest / "checkpoints.db-wal").exists()


def test_backup_cli(tmp_path: Path) -> None:
    root = tmp_path / ".recertia"
    root.mkdir()
    (root / "x").write_text("y", encoding="utf-8")
    archive = tmp_path / "out.tar.gz"
    created = runner.invoke(
        app, ["backup", "create", "--root", str(root), "--output", str(archive)]
    )
    assert created.exit_code == 0, created.output
    archive_default = tmp_path / "out-default.tar.gz"
    created_default = runner.invoke(
        app, ["backup", "--root", str(root), "--output", str(archive_default)]
    )
    assert created_default.exit_code == 0, created_default.output
    dest = tmp_path / "restored"
    restored = runner.invoke(app, ["restore", str(archive), "--dest", str(dest)])
    assert restored.exit_code == 0, restored.output
    assert (dest / "x").read_text(encoding="utf-8") == "y"


def test_tabletop_walks_run_and_restore(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    spec = {
        "task": {"request": "write output.txt"},
        "criteria": [
            {
                "id": "output-exists",
                "kind": "command",
                "run": "test -f output.txt",
                "source": "caller",
                "weight": 1.0,
                "sensitivity_proof": {
                    "criterion_id": "output-exists",
                    "negative_fixture": "empty workspace",
                    "rejected": True,
                    "checked_at": "2026-01-01T00:00:00Z",
                },
            }
        ],
        "script": ["python3 -c \"open('output.txt','w').write('done')\""],
        "workdir": str(workdir),
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    runs_root = tmp_path / "runs"
    run = runner.invoke(
        app,
        ["run", "--spec", str(spec_path), "--runs-root", str(runs_root), "--run-id", "tt-run-1"],
    )
    assert run.exit_code == 0, run.output

    archive = tmp_path / "backup.tar.gz"
    backup_tree(runs_root, archive)
    log_path = tmp_path / "tabletop.json"
    result = runner.invoke(
        app,
        [
            "tabletop",
            "tt-run-1",
            "--runs-root",
            str(runs_root),
            "--restore-from",
            str(archive),
            "--restore-dest",
            str(tmp_path / "restored-runs"),
            "--follow-up",
            "accepted restore path",
            "--output",
            str(log_path),
        ],
    )
    assert result.exit_code == 0, result.output
    log = json.loads(log_path.read_text(encoding="utf-8"))
    assert log["run_id"] == "tt-run-1"
    assert log["ga_claimed"] is False
    assert log["restore_ok"] is True
    assert log["terminal"] == "solved"
    assert log["navigable"] is True
    assert (tmp_path / "restored-runs").is_dir()


def test_synthetic_canary_and_live_stub_verifier() -> None:
    synthetic = run_judge_canary()
    assert synthetic.trials >= 1
    assert synthetic.false_passes == 0
    assert synthetic.mode == "synthetic"

    fail_client = StubModelClient(responses=["FAIL"], provider="stub", model_id="canary-v")
    live = run_live_verifier_canary(verifier=fail_client, model_version="stub × canary-v")
    assert live.mode == "live"
    assert live.trials >= 1
    assert live.false_passes == 0
    assert live.attribution == "stub × canary-v"

    pass_client = StubModelClient(responses=["PASS"], provider="stub", model_id="canary-v")
    poisoned = run_live_verifier_canary(verifier=pass_client, model_version="stub × canary-v")
    assert poisoned.false_passes >= 1


def test_live_canary_cli_refuses_without_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECERTIA_VERIFIER_MODEL_ID", raising=False)
    monkeypatch.delenv("RECERTIA_MODEL_PROVIDER", raising=False)
    result = runner.invoke(app, ["canary", "--live"])
    assert result.exit_code == 2
    assert "RECERTIA_VERIFIER_MODEL_ID" in result.output

    synthetic = runner.invoke(app, ["canary"])
    assert synthetic.exit_code == 0, synthetic.output
    payload = json.loads(synthetic.output)
    assert payload["a4_updated"] is False
    assert payload["trials"] >= 1


def test_live_canary_error_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECERTIA_VERIFIER_MODEL_ID", raising=False)
    monkeypatch.setenv("RECERTIA_MODEL_PROVIDER", "stub")
    try:
        run_live_verifier_canary()
        raise AssertionError("expected LiveCanaryError")
    except LiveCanaryError:
        pass


def test_weekly_ops_does_not_swallow_failures() -> None:
    text = Path(".github/workflows/weekly-ops.yml").read_text(encoding="utf-8")
    assert "|| true" not in text
    assert "recertia eval run" in text
    assert "recertia canary" in text
    assert "RECERTIA_VERIFIER_MODEL_ID" in text
    assert "recertia soak record" in text
    assert "recertia soak status" not in text


def _empty_weekly() -> dict:
    return {
        "report": {
            "snapshot_id": "none",
            "first_attempt_success": None,
            "reuse_rate": None,
            "attempts_to_success": None,
            "cost_per_solved_task": None,
            "causal_lift": {
                "status": "insufficient_data",
                "treatment": {"successes": 0, "trials": 0},
                "control": {"successes": 0, "trials": 0},
            },
            "unavailable": {"causal_lift": "insufficient_data"},
            "at": "2026-08-17T12:00:00+00:00",
        },
        "claim": "insufficient_data",
        "canary": {"trials": 2, "false_passes": 0, "attribution": "synthetic"},
    }


def _live_weekly(*, treatment: int = 8, control: int = 8) -> dict:
    return {
        "report": {
            "snapshot_id": "soak",
            "first_attempt_success": 0.5,
            "reuse_rate": 0.25,
            "attempts_to_success": 1.4,
            "cost_per_solved_task": 0.12,
            "retrieval_precision_at_3": 0.8,
            "causal_lift": {
                "status": "not_established",
                "treatment": {"successes": 4, "trials": treatment},
                "control": {"successes": 4, "trials": control},
                "interval": {"low": -0.2, "high": 0.2, "level": 0.95},
            },
            "unavailable": {},
            "at": "2026-08-17T12:00:00+00:00",
        },
        "claim": "not established",
        "canary": {"trials": 2, "false_passes": 0, "attribution": "openai × gpt-4o"},
    }


def test_empty_eval_week_is_recorded_and_not_counted(tmp_path: Path) -> None:
    from recertia.ops.soak import classify_week

    week = classify_week(_empty_weekly(), week="2026-W34")
    assert week.counted is False
    assert week.reason == "empty_eval_db"
    assert week.as_dict()["ga_claimed"] is False

    metrics = tmp_path / "weekly.json"
    metrics.write_text(json.dumps(_empty_weekly()), encoding="utf-8")
    log = tmp_path / "soak-log.json"
    recorded = runner.invoke(
        app,
        ["soak", "record", "--metrics", str(metrics), "--log", str(log), "--week", "2026-W34"],
    )
    assert recorded.exit_code == 0, recorded.output
    assert "empty_eval_db" in recorded.output
    stored = json.loads(log.read_text(encoding="utf-8"))
    assert stored["ga_claimed"] is False
    assert stored["weeks"][0]["counted"] is False


def test_four_consecutive_live_weeks_plus_tabletop_is_gate_ready(tmp_path: Path) -> None:
    log = tmp_path / "soak-log.json"
    for week in ("2026-W31", "2026-W32", "2026-W33", "2026-W34"):
        metrics = tmp_path / f"{week}.json"
        metrics.write_text(json.dumps(_live_weekly()), encoding="utf-8")
        recorded = runner.invoke(
            app,
            ["soak", "record", "--metrics", str(metrics), "--log", str(log), "--week", week],
        )
        assert recorded.exit_code == 0, recorded.output
        assert json.loads(recorded.output)["counted"] is True

    tabletop = tmp_path / "tabletop.json"
    tabletop.write_text(
        json.dumps({"pass": True, "ga_claimed": False, "run_id": "soak-run-1"}),
        encoding="utf-8",
    )
    ready = runner.invoke(
        app, ["soak", "status", "--log", str(log), "--tabletop", str(tabletop)]
    )
    assert ready.exit_code == 0, ready.output
    payload = json.loads(ready.output)
    assert payload["counted_weeks"] == 4
    assert payload["gate_ready"] is True
    assert payload["ga_claimed"] is False


def test_broken_streak_and_tabletop_ga_claim_block_the_gate(tmp_path: Path) -> None:
    from recertia.ops.soak import classify_week, consecutive_counted, record_week, status

    log: dict = {"weeks": [], "ga_claimed": False}
    for week in ("2026-W31", "2026-W33"):
        log = record_week(log, classify_week(_live_weekly(), week=week))
    assert consecutive_counted(log["weeks"]) == 1

    blocked = status(log, tabletop={"pass": True, "ga_claimed": True})
    assert blocked["gate_ready"] is False
    assert "tabletop_claimed_ga" in blocked["missing"]
    assert blocked["ga_claimed"] is False
