"""Incident tabletop walker (operator GA). Does not declare GA."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recertia.graph.store import CheckpointStore
from recertia.ids import InvalidIdError, validate_run_id
from recertia.ledger import HashChainLedger, LedgerVerificationError
from recertia.ops.backup import BackupError, restore_tree
from recertia.persistence.import_legacy import live_checkpoint_db
from recertia.solver.transcript import TranscriptStore


def _ledger_paths(runs_root: Path, tenant: str) -> list[Path]:
    root = Path(runs_root)
    return [
        root / "ledger.jsonl",
        root / "runs" / tenant / "ledger.jsonl",
    ]


def _orchestrator_roots(runs_root: Path, tenant: str) -> list[Path]:
    root = Path(runs_root)
    return [root, root / "runs" / tenant]


def inspect_run(run_id: str, *, runs_root: Path, tenant: str = "default") -> dict[str, Any]:
    """Ledger → checkpoint/transcript → failure class → manifest pins."""

    try:
        run_id = validate_run_id(run_id)
    except InvalidIdError as exc:
        raise ValueError(str(exc)) from exc

    payload: dict[str, Any] = {
        "run_id": run_id,
        "ledger_ok": False,
        "ledger_hits": 0,
        "ledger_path": None,
        "transcript_found": False,
        "transcript_ref": None,
        "failure_class": None,
        "terminal": None,
        "manifest": None,
        "navigable": False,
    }

    found_ledger = False
    for path in _ledger_paths(runs_root, tenant):
        if not path.exists():
            continue
        found_ledger = True
        # Only construct HashChainLedger when the file exists — __init__ mkdir's parents.
        ledger = HashChainLedger(path)
        try:
            ledger.verify()
            payload["ledger_ok"] = True
        except LedgerVerificationError as exc:
            payload["ledger_error"] = str(exc)
            payload["ledger_ok"] = False
        hits = [
            e.model_dump(mode="json")
            for e in ledger.entries()
            if run_id in e.target or run_id in str(e.evidence)
        ]
        payload["ledger_hits"] = len(hits)
        payload["ledger_path"] = str(path)
        break
    if not found_ledger:
        # Empty chain verifies (same as `recertia ledger verify` on a fresh root).
        payload["ledger_ok"] = True
        payload["ledger_hits"] = 0

    state = None
    for orch_root in _orchestrator_roots(runs_root, tenant):
        db = live_checkpoint_db(orch_root)
        if db is None:
            continue
        store = CheckpointStore(db)
        try:
            latest = store.latest(run_id)
            if latest is None:
                continue
            _seq, _node, _nxt, state = latest
            payload["checkpoint_root"] = str(orch_root)
            break
        finally:
            store.close()

    if state is None:
        payload["error"] = "run not found in checkpoints"
        return payload

    payload["terminal"] = state.terminal
    payload["attempt_no"] = state.attempt_no
    if state.failure is not None:
        payload["failure_class"] = state.failure.failure_class
    payload["manifest"] = state.manifest.model_dump(mode="json")
    payload["transcript_ref"] = state.transcript_ref
    if state.transcript_ref:
        for orch_root in _orchestrator_roots(runs_root, tenant):
            transcripts_dir = orch_root / "transcripts"
            if not transcripts_dir.is_dir():
                continue
            transcripts = TranscriptStore(transcripts_dir)
            try:
                transcripts.read(state.transcript_ref)
                payload["transcript_found"] = True
                break
            except FileNotFoundError:
                continue

    payload["navigable"] = bool(
        payload["ledger_ok"] and (payload["failure_class"] or payload["terminal"])
    )
    return payload


def run_tabletop(
    run_id: str,
    *,
    runs_root: Path,
    tenant: str = "default",
    restore_from: Path | None = None,
    restore_dest: Path | None = None,
    follow_up: str = "",
) -> dict[str, Any]:
    """Walk the tabletop path and optionally restore a backup. Never sets GA."""

    started = time.monotonic()
    at = datetime.now(timezone.utc)
    inspection = inspect_run(run_id, runs_root=runs_root, tenant=tenant)
    restore_meta: dict[str, Any] = {
        "restore_source": str(restore_from) if restore_from else None,
        "restore_ok": None,
    }
    if restore_from is not None:
        dest = restore_dest
        if dest is None:
            dest = Path(runs_root).resolve().parent / "recertia-restore"
        try:
            restore_tree(Path(restore_from), dest, overwrite=True)
            restore_meta["restore_ok"] = True
            restore_meta["restore_dest"] = str(dest)
            restore_meta["usable_tree"] = dest.is_dir() and any(dest.iterdir())
        except BackupError as exc:
            restore_meta["restore_ok"] = False
            restore_meta["restore_error"] = str(exc)
            restore_meta["usable_tree"] = False
    ttr_s = round(time.monotonic() - started, 3)
    log = {
        "date": at.isoformat(),
        "run_id": run_id,
        "ttr_s": ttr_s,
        "follow_up": follow_up,
        "ga_claimed": False,
        **inspection,
        **restore_meta,
    }
    if restore_from is not None and restore_meta.get("restore_ok") and inspection.get("navigable"):
        log["pass"] = True
    else:
        log["pass"] = bool(inspection.get("navigable")) and restore_from is None
    return log
