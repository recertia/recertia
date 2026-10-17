"""Store-receipt index (ADR-0020 P1).

``close_receipt`` is the only appender of ``action="close_receipt"``. The ledger
is the chain; ``receipts.jsonl`` is a derived dual-write next to ``ledger.jsonl``.
P1 does not pack residue, flip backup defaults, or close receipts from ``store``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from contracts.ledger import LedgerEntry
from contracts.receipt import ResidueManifestEntry, StoreReceipt
from recertia.errors import RecertiaError
from recertia.fsutil.atomic import Durability
from recertia.fsutil.jsonl import JsonlAppender, iter_complete_lines
from recertia.ledger import HashChainLedger

GENESIS_RECEIPT_HASH = "0" * 64
_OPERATOR_PLANE = (
    "api_keys.sqlite",
    "job_quota.json",
    "jobs/job_quota.json",
)


class ReceiptError(RecertiaError):
    """Invalid receipt close, index, or disk alarm."""

    code = "receipt_error"
    http_status = 400
    retryable = False
    exit_code = 2


def close_receipt(
    ledger: HashChainLedger,
    store_receipt: StoreReceipt,
    *,
    receipts_path: Path,
    actor: str,
) -> LedgerEntry:
    """Append ``close_receipt`` and dual-write one JSON line to ``receipts.jsonl``.

    This is the only call site that appends ``action="close_receipt"``.
    """

    evidence = store_receipt.model_dump(mode="json")
    try:
        StoreReceipt.model_validate(evidence)
    except Exception as exc:
        raise ReceiptError(f"close_receipt evidence is not a StoreReceipt: {exc}") from exc
    entry = ledger.append(
        actor=actor,
        action="close_receipt",
        target=store_receipt.receipt_id,
        evidence=evidence,
    )
    if store_receipt.ledger_seq is None:
        evidence["ledger_seq"] = entry.seq
    line = (json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    appender = JsonlAppender(
        Path(receipts_path),
        durability=_ledger_durability(ledger),
        fsync_each=True,
        lock_timeout_s=getattr(ledger, "_lock_timeout_s", 5.0),
    )
    appender.append(line)
    return entry


def inventory_tree(root: Path) -> list[ResidueManifestEntry]:
    """Path+hash+bytes of files under ``root``. Skips lock files and SQLite sidecars."""

    base = Path(root)
    if not base.is_dir():
        return []
    entries: list[ResidueManifestEntry] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if _skip_inventory(path):
            continue
        rel = path.relative_to(base).as_posix()
        entries.append(_file_entry(path, rel))
    return entries


def reviewed_git_head(skills_root: Path | None = None) -> str | None:
    """HEAD of the product checkout that owns ``skills/``. Errors become ``None``."""

    start = Path.cwd() if skills_root is None else Path(skills_root)
    try:
        proc = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--verify", "--end-of-options", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    sha = proc.stdout.strip()
    return sha or None


def assert_receipt_disk(root: Path) -> None:
    """Refuse close when free disk on the runs root is below 2× current tree size."""

    base = Path(root)
    probe = base if base.exists() else base.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        raise ReceiptError(f"cannot measure disk usage for {probe}: {exc}") from exc
    size = _tree_size_bytes(base)
    if usage.free < 2 * size:
        raise ReceiptError(
            f"free disk {usage.free} bytes is below 2× tree size {size} bytes under {base}"
        )


def load_receipts(receipts_path: Path) -> list[StoreReceipt]:
    """Parse complete ``receipts.jsonl`` lines. Missing file is empty."""

    path = Path(receipts_path)
    receipts: list[StoreReceipt] = []
    for raw in iter_complete_lines(path):
        try:
            receipts.append(StoreReceipt.model_validate_json(raw))
        except Exception as exc:
            raise ReceiptError(f"receipts.jsonl line does not parse: {exc}") from exc
    return receipts


def last_receipt(receipts_path: Path) -> StoreReceipt | None:
    receipts = load_receipts(receipts_path)
    return receipts[-1] if receipts else None


def build_close_receipt(
    root: Path,
    *,
    genesis: bool,
    receipts_path: Path,
    skills_root: Path | None = None,
) -> StoreReceipt:
    """Inventory the live tree for ``receipt close`` (genesis or operator)."""

    base = Path(root)
    if genesis:
        assert_receipt_disk(base)
    manifest = _with_operator_plane(base, inventory_tree(base))
    prev = last_receipt(receipts_path)
    seq = 0 if prev is None else prev.seq + 1
    prev_hash = GENESIS_RECEIPT_HASH if prev is None else prev.receipt_hash
    closed_by: Literal["upgrade_genesis", "operator"] = (
        "upgrade_genesis" if genesis else "operator"
    )
    kind = "genesis" if genesis else "receipt"
    draft = StoreReceipt(
        receipt_id=f"{kind}-{seq}",
        seq=seq,
        prev_receipt_hash=prev_hash,
        receipt_hash="pending",
        closed_by=closed_by,
        genesis=genesis,
        reviewed_git=reviewed_git_head(skills_root),
        fold_digest=None,
        residue_cid=None,
        residue_manifest=manifest,
        compression_moves=[],
        ledger_seq=None,
        at=datetime.now(timezone.utc),
    )
    return draft.model_copy(update={"receipt_hash": _receipt_hash(draft)})


def has_genesis(receipts: list[StoreReceipt]) -> bool:
    return any(item.genesis or item.closed_by == "upgrade_genesis" for item in receipts)


def verify_close_receipt_evidence(evidence: Mapping[str, Any]) -> StoreReceipt:
    payload = {key: value for key, value in evidence.items() if key != "_ledger_sig"}
    try:
        return StoreReceipt.model_validate(payload)
    except Exception as exc:
        raise ReceiptError(f"close_receipt evidence is not a StoreReceipt: {exc}") from exc


def _receipt_hash(receipt: StoreReceipt) -> str:
    payload = receipt.model_dump(mode="json", exclude={"receipt_hash"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _ledger_durability(ledger: HashChainLedger) -> Durability:
    existing = getattr(ledger, "_durability", None)
    if isinstance(existing, Durability):
        return existing
    return Durability(fsync_file=True, fsync_dir=True)


def _skip_inventory(path: Path) -> bool:
    name = path.name
    if name == ".lock" or name.endswith(".lock"):
        return True
    if name.endswith("-wal") or name.endswith("-shm"):
        return True
    return False


def _file_entry(path: Path, rel: str) -> ResidueManifestEntry:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return ResidueManifestEntry(path=rel, content_hash=digest.hexdigest(), bytes=size)


def _with_operator_plane(root: Path, entries: list[ResidueManifestEntry]) -> list[ResidueManifestEntry]:
    seen = {item.path for item in entries}
    extra = list(entries)
    for rel in _OPERATOR_PLANE:
        path = root / rel
        if not path.is_file() or path.is_symlink() or _skip_inventory(path):
            continue
        if rel in seen:
            continue
        extra.append(_file_entry(path, rel))
        seen.add(rel)
    return extra


def _tree_size_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    total = 0
    if root.is_file():
        return root.stat().st_size
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total
