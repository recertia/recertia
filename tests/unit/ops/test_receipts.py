"""Store-receipt P1: close_receipt dual-write, genesis inventory, backup of receipts.jsonl."""

from __future__ import annotations

import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from contracts.receipt import StoreReceipt
from recertia.cli.main import app
from recertia.ledger import HashChainLedger, LedgerVerificationError
from recertia.ops.backup import backup_tree, verify_archive
from recertia.ops.receipts import (
    build_close_receipt,
    close_receipt,
    inventory_tree,
    load_receipts,
)
from recertia.persistence.layout import RuntimeLayout

runner = CliRunner()


def _receipt(**overrides: object) -> StoreReceipt:
    payload: dict[str, object] = {
        "receipt_id": "rcpt-1",
        "seq": 0,
        "prev_receipt_hash": "0" * 64,
        "receipt_hash": "a" * 64,
        "closed_by": "operator",
        "at": datetime(2026, 9, 23, tzinfo=timezone.utc),
    }
    payload.update(overrides)
    return StoreReceipt.model_validate(payload)


def test_close_writes_ledger_and_receipts_jsonl(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path).local_runs()
    ledger = HashChainLedger(layout.ledger_path)
    receipt = _receipt(compression_moves=[])
    entry = close_receipt(
        ledger, receipt, receipts_path=layout.receipts_path, actor="operator"
    )
    assert entry.action == "close_receipt"
    assert entry.target == receipt.receipt_id
    evidence = StoreReceipt.model_validate(
        {k: v for k, v in entry.evidence.items() if k != "_ledger_sig"}
    )
    assert evidence.compression_moves == []
    assert evidence.receipt_id == "rcpt-1"
    lines = layout.receipts_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    indexed = StoreReceipt.model_validate_json(lines[0])
    assert indexed.compression_moves == []
    assert indexed.ledger_seq == entry.seq
    ledger.verify()


def test_genesis_inventories_files_and_sets_upgrade_genesis(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    (root / "note.txt").write_text("keep-me\n", encoding="utf-8")
    (root / "api_keys.sqlite").write_bytes(b"keys")
    (root / "jobs").mkdir()
    (root / "jobs" / "job_quota.json").write_text("{}", encoding="utf-8")
    (root / "ledger.jsonl.lock").write_text("lock", encoding="utf-8")
    (root / "db.sqlite-wal").write_text("wal", encoding="utf-8")
    layout = RuntimeLayout(root).local_runs()
    receipt = build_close_receipt(
        root, genesis=True, receipts_path=layout.receipts_path, skills_root=Path("/tmp/wt-sr")
    )
    assert receipt.closed_by == "upgrade_genesis"
    assert receipt.genesis is True
    assert receipt.fold_digest is None
    assert receipt.residue_cid is None
    assert receipt.compression_moves == []
    paths = {item.path for item in receipt.residue_manifest}
    assert "note.txt" in paths
    assert "api_keys.sqlite" in paths
    assert "jobs/job_quota.json" in paths
    assert "ledger.jsonl.lock" not in paths
    assert "db.sqlite-wal" not in paths
    inventoried = inventory_tree(root)
    assert all(not item.path.endswith(".lock") for item in inventoried)
    ledger = HashChainLedger(layout.ledger_path)
    close_receipt(ledger, receipt, receipts_path=layout.receipts_path, actor="upgrade")
    stored = load_receipts(layout.receipts_path)
    assert stored[0].closed_by == "upgrade_genesis"
    assert stored[0].ledger_seq == 0


def test_store_write_does_not_emit_close_receipt(tmp_path: Path) -> None:
    from contracts.criteria import SensitivityProof, SkillCertificationCriterion
    from contracts.run import RunState, Task
    from contracts.skill import Hygiene, Provenance, SkillVersion, Step
    from recertia.graph.ops import OperationLedger
    from recertia.memory.procedural.store import SkillStore
    from recertia.nodes.context import NodeContext
    from recertia.nodes.store import store
    from recertia.workspace import WorkspaceManager

    proof = SensitivityProof(
        criterion_id="ok",
        negative_fixture="empty",
        rejected=True,
        checked_at=datetime.now(timezone.utc),
    )
    version = SkillVersion(
        skill_id="unit-demo-skill",
        version=1,
        title="Unit demo skill title",
        intent="Intent long enough for the skill version contract minimum.",
        task_class="repo-chore",
        steps=[
            Step(
                id="step_1",
                tool="shell",
                intent="Write a trivial marker file into the workspace",
                inputs={"command": "echo hi > output.txt"},
            )
        ],
        certification_criteria=[
            SkillCertificationCriterion(
                id="ok",
                kind="command",
                run="test -f output.txt",
                sensitivity_proof=proof,
                preregistered=True,
            )
        ],
        provenance=Provenance(
            distilled_from_run="unit-run",
            distilled_at=datetime.now(timezone.utc),
            curation="self_distilled",
            authoring_prior_version="ap-test",
        ),
        hygiene=Hygiene(secret_scan="skipped"),
    )
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    ledger = HashChainLedger(tmp_path / "ledger.jsonl")
    ctx = NodeContext(
        run_id="unit-run",
        attempt_no=0,
        node="store",
        workdir=workdir,
        workspaces=WorkspaceManager(tmp_path / "snapshots"),
        ledger=ledger,
        ops=OperationLedger(tmp_path / "ops.db"),
        script=None,
        store=SkillStore(tmp_path / "skills"),
    )
    state = RunState(
        run_id="unit-run",
        task=Task(task_id="t", request="do a thing", submitted_at=datetime.now(timezone.utc)),
        draft=version.model_dump(mode="json"),
    )
    outcome = store(state, ctx)
    assert outcome.route == "always"
    actions = [entry.action for entry in ledger.entries()]
    assert "write" in actions
    assert "close_receipt" not in actions


def test_backup_tree_includes_receipts_jsonl(tmp_path: Path) -> None:
    root = tmp_path / "recertia"
    root.mkdir()
    (root / "receipts.jsonl").write_text(
        json.dumps({"receipt_id": "rcpt-1"}) + "\n", encoding="utf-8"
    )
    archive = tmp_path / "backups" / "recertia.tar.gz"
    backup_tree(root, archive)
    manifest = verify_archive(archive)
    paths = [item["path"] for item in manifest["files"]]
    assert "receipts.jsonl" in paths
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    assert "receipts.jsonl" in names


def test_invalid_close_receipt_evidence_fails_ledger_verify(tmp_path: Path) -> None:
    ledger = HashChainLedger(tmp_path / "ledger.jsonl")
    ledger.append(actor="a", action="close_receipt", target="x", evidence={})
    try:
        ledger.verify(full=True)
    except LedgerVerificationError as exc:
        assert "StoreReceipt" in str(exc)
    else:
        raise AssertionError("expected LedgerVerificationError for invalid close_receipt")


def test_receipt_cli_genesis_verify_show(tmp_path: Path) -> None:
    root = tmp_path / ".recertia"
    root.mkdir()
    (root / "note.txt").write_text("present\n", encoding="utf-8")
    closed = runner.invoke(
        app,
        [
            "receipt",
            "close",
            "--genesis",
            "--runs-root",
            str(root),
            "--actor",
            "upgrade",
            "--skills-root",
            "/tmp/wt-sr",
        ],
    )
    assert closed.exit_code == 0, closed.output
    assert "upgrade_genesis" in closed.output
    verified = runner.invoke(app, ["receipt", "verify", "--runs-root", str(root)])
    assert verified.exit_code == 0, verified.output
    assert "receipts OK" in verified.output
    shown = runner.invoke(app, ["receipt", "show", "--runs-root", str(root)])
    assert shown.exit_code == 0, shown.output
    payload = json.loads(shown.output)
    assert payload["receipts"][0]["closed_by"] == "upgrade_genesis"
    assert payload["receipts"][0]["genesis"] is True
    assert any(item["path"] == "note.txt" for item in payload["receipts"][0]["residue_manifest"])

    empty = tmp_path / "empty-root"
    empty.mkdir()
    missing = runner.invoke(app, ["receipt", "verify", "--runs-root", str(empty)])
    assert missing.exit_code == 0, missing.output
    assert "no genesis receipt" in missing.output
