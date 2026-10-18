"""Hash-chain mechanics for the provenance ledger (specs §21).

Storage is an append-only JSONL file: one ``LedgerEntry`` per line, in ``seq`` order.
Append holds a cross-process flock across tip re-read, write, and fsync. A torn tail
is refused until ``recertia ledger repair --truncate-torn-tail``. A verified sidecar
lets ``verify(full=False)`` scan only the suffix.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from contracts.ledger import LedgerAction, LedgerEntry
from recertia.faults import fault_point
from recertia.fsutil.atomic import Durability
from recertia.fsutil.jsonl import JsonlAppender
from recertia.fsutil.locks import file_lock
from recertia.ledger.repair import RepairReport
from recertia.ledger.sidecar import load_sidecar, sidecar_matches, sidecar_path, write_sidecar
from recertia.runtime.errors import LedgerUnavailable

GENESIS_HASH = "0" * 64
_TORN = "torn tail; run `recertia ledger repair --truncate-torn-tail`"


class LedgerVerificationError(Exception):
    """The chain does not verify: a hash mismatch, a broken link, or a gap in ``seq``."""


class LedgerHook(Protocol):
    """SEC-6 may sign inside ``before_append``. The writer stays one implementation."""

    def before_append(self, entry: LedgerEntry) -> LedgerEntry:
        """Return the entry to hash."""

    def after_append(self, entry: LedgerEntry) -> None:
        """Called after the line and the sidecar are durable."""


@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    entries: int
    full: bool
    suffix_entries: int
    tip_hash: str


def _canonical_bytes(entry: LedgerEntry) -> bytes:
    """Canonical serialisation of every field except ``entry_hash`` (specs §21)."""

    payload = entry.model_dump(mode="json", exclude={"entry_hash"})
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_entry_hash(entry: LedgerEntry) -> str:
    return hashlib.sha256(_canonical_bytes(entry)).hexdigest()


def read_tail(path: Path) -> tuple[LedgerEntry | None, int]:
    """Return ``(last complete entry, byte length of the complete prefix)``.

    A trailing partial line is excluded from the length. A complete line that
    does not parse raises ``LedgerUnavailable``.
    """

    if not path.exists():
        return None, 0
    size = path.stat().st_size
    if size == 0:
        return None, 0
    window = 1024 * 1024
    with path.open("rb") as handle:
        end = size
        data = b""
        start = 0
        while True:
            start = max(0, end - window)
            handle.seek(start)
            data = handle.read(end - start) + data
            end = start
            if data.count(b"\n") >= 2 or start == 0:
                break
            window *= 2
    last_nl = data.rfind(b"\n")
    if last_nl < 0:
        return None, 0
    base = size - len(data)
    complete_len = base + last_nl + 1
    prev_nl = data.rfind(b"\n", 0, last_nl)
    line = data[:last_nl] if prev_nl < 0 else data[prev_nl + 1 : last_nl]
    if not line.strip():
        # Extra trailing newlines. Walk back to the previous non-empty record.
        cursor = prev_nl
        while cursor >= 0:
            earlier = data.rfind(b"\n", 0, cursor)
            candidate = data[:cursor] if earlier < 0 else data[earlier + 1 : cursor]
            if candidate.strip():
                line = candidate
                break
            if earlier < 0:
                line = b""
                break
            cursor = earlier
    if not line.strip():
        return None, complete_len
    try:
        entry = LedgerEntry.model_validate_json(line)
    except Exception as exc:
        raise LedgerUnavailable(f"ledger line does not parse: {exc}") from exc
    return entry, complete_len


class HashChainLedger:
    """One ledger, backed by one append-only JSONL file.

    The flock covers tip re-read, append, and fsync. ``verify(full=False)`` trusts
    a sidecar only when the prefix SHA-256 matches, then checks the new suffix.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        durability: str | Durability | None = None,
        hook: LedgerHook | None = None,
        lock_timeout_s: float | None = None,
        lease_db: Path | str | None = None,
        tenant_id: str = "local",
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lease_db = Path(lease_db) if lease_db is not None else None
        self._tenant_id = tenant_id
        self._default_fencing_token: Callable[[], int | None] | None = None
        self._hook = hook
        self._tip: tuple[int, str] | None = None
        self.lines_parsed = 0
        name, timeout = _resolve_durability(durability, lock_timeout_s)
        self._durability_name = name
        self._durability = (
            durability
            if isinstance(durability, Durability)
            else Durability(fsync_file=name != "fast", fsync_dir=name != "fast")
        )
        self._lock_timeout_s = timeout

    @property
    def path(self) -> Path:
        return self._path

    def _lock_path(self) -> Path:
        return self._path.with_name(self._path.name + ".lock")

    def _held(self):
        return file_lock(self._lock_path(), timeout_s=self._lock_timeout_s)

    def tip_hash(self) -> str:
        with self._held():
            last, _complete = read_tail(self._path)
            if last is None:
                return GENESIS_HASH
            return last.entry_hash

    def append(
        self,
        *,
        actor: str,
        action: LedgerAction,
        target: str,
        evidence: dict | None = None,
        at: datetime | None = None,
        fencing_token: int | None = None,
    ) -> LedgerEntry:
        """Append one entry. Returns the entry as written, with ``seq`` and hashes filled in."""

        if fencing_token is None and self._default_fencing_token is not None:
            fencing_token = self._default_fencing_token()
        with self._held():
            self._require_run_fence(target, fencing_token)
            return self._append_locked(
                actor=actor,
                action=action,
                target=target,
                evidence=evidence,
                at=at,
            )

    def set_default_fencing_token(self, getter: Callable[[], int | None] | None) -> None:
        """Use the current run's token when ``append`` is not given one (engine path)."""

        self._default_fencing_token = getter

    def _require_run_fence(self, target: str, fencing_token: int | None) -> None:
        """Reject appends that target a currently held run without a matching token.

        Skill ids and other non-run targets have no ``run_leases`` row, so a
        missing row (or missing sibling ``runtime.db``) is allowed.
        """

        from recertia.persistence.fence import require_lease_token

        lease_db = self._lease_db if self._lease_db is not None else self._path.parent / "runtime.db"
        require_lease_token(
            lease_db,
            tenant_id=self._tenant_id,
            run_id=target,
            fencing_token=fencing_token,
            missing_row="allow",
        )

    def _append_locked(
        self,
        *,
        actor: str,
        action: LedgerAction,
        target: str,
        evidence: dict | None = None,
        at: datetime | None = None,
    ) -> LedgerEntry:
        seq, prev_hash = self._tip_for_append()
        draft = LedgerEntry(
            seq=seq,
            prev_hash=prev_hash,
            entry_hash="pending",
            actor=actor,
            action=action,
            target=target,
            evidence=evidence or {},
            at=at or datetime.now(timezone.utc),
        )
        hook = self._hook if self._hook is not None else _settings_hook()
        if hook is not None:
            draft = hook.before_append(draft)
        entry = draft.model_copy(update={"entry_hash": compute_entry_hash(draft)})
        line = (entry.model_dump_json() + "\n").encode("utf-8")
        appender = JsonlAppender(
            self._path,
            durability=self._durability,
            fsync_each=self._durability.fsync_file,
            lock_timeout_s=self._lock_timeout_s,
        )
        appender.append_unlocked(
            line,
            before_fsync=lambda: fault_point("ledger.after_write_before_fsync"),
        )
        self._tip = (seq + 1, entry.entry_hash)
        write_sidecar(
            self._path,
            entries=seq + 1,
            tip_hash=entry.entry_hash,
            durability_name=self._durability_name,
            durability=self._durability,
        )
        if hook is not None:
            hook.after_append(entry)
        return entry

    def _tip_for_append(self) -> tuple[int, str]:
        last, complete_len = read_tail(self._path)
        size = self._path.stat().st_size if self._path.exists() else 0
        if size > complete_len:
            raise LedgerUnavailable(_TORN)
        observed = (0, GENESIS_HASH) if last is None else (last.seq + 1, last.entry_hash)
        if self._tip is not None and self._tip != observed:
            observed = self._scan_tip(complete_len)
        elif self._tip is None:
            side = load_sidecar(sidecar_path(self._path))
            trusted = (
                side is not None
                and sidecar_matches(self._path, side)
                and (side.next_seq, side.tip_hash) == observed
            )
            if not trusted:
                observed = self._scan_tip(complete_len)
        self._tip = observed
        return observed

    def _scan_tip(self, complete_len: int) -> tuple[int, str]:
        raw = self._path.read_bytes()[:complete_len] if self._path.exists() else b""
        entries = self._entries_from_bytes(raw)
        if not entries:
            return (0, GENESIS_HASH)
        return (len(entries), entries[-1].entry_hash)

    def entries(self) -> list[LedgerEntry]:
        with self._held():
            size = self._path.stat().st_size if self._path.exists() else 0
            _last, complete_len = read_tail(self._path)
            if size > complete_len:
                raise LedgerVerificationError(_TORN)
            raw = self._path.read_bytes() if size else b""
            return self._entries_from_bytes(raw)

    def verify(self, *, full: bool = False, check_sig: bool = False) -> VerifyReport:
        """Recompute the chain. Raise :class:`LedgerVerificationError` on the first break.

        ``full=False`` uses a verified sidecar and checks only the suffix. A torn
        tail always fails, including when the complete prefix would otherwise verify.
        ``check_sig=True`` also checks HMAC signatures and forces a full scan.
        Unsigned entries are accepted until the first signed entry.
        """

        with self._held():
            return self._verify_unlocked(full=full or check_sig, check_sig=check_sig)

    def _verify_unlocked(self, *, full: bool, check_sig: bool = False) -> VerifyReport:
        self.lines_parsed = 0
        size = self._path.stat().st_size if self._path.exists() else 0
        try:
            _last, complete_len = read_tail(self._path)
        except LedgerUnavailable as exc:
            raise LedgerVerificationError(str(exc)) from exc
        if size > complete_len:
            raise LedgerVerificationError(_TORN)
        raw = self._path.read_bytes() if size else b""
        side = None if full else load_sidecar(sidecar_path(self._path))
        if side is not None and sidecar_matches(self._path, side) and side.prefix_bytes <= complete_len:
            suffix = raw[side.prefix_bytes : complete_len]
            suffix_entries = self._entries_from_bytes(suffix)
            self._verify_chain(suffix_entries, start_seq=side.entries, prev_hash=side.tip_hash)
            tip = suffix_entries[-1].entry_hash if suffix_entries else side.tip_hash
            total = side.entries + len(suffix_entries)
            self._tip = (total, tip)
            if suffix_entries:
                write_sidecar(
                    self._path,
                    entries=total,
                    tip_hash=tip,
                    durability_name=self._durability_name,
                    durability=self._durability,
                )
            return VerifyReport(
                ok=True,
                entries=total,
                full=False,
                suffix_entries=len(suffix_entries),
                tip_hash=tip,
            )
        entries = self._entries_from_bytes(raw[:complete_len])
        self._verify_chain(entries, start_seq=0, prev_hash=GENESIS_HASH)
        if check_sig:
            self._check_signatures(entries)
        tip = entries[-1].entry_hash if entries else GENESIS_HASH
        self._tip = (len(entries), tip)
        write_sidecar(
            self._path,
            entries=len(entries),
            tip_hash=tip,
            durability_name=self._durability_name,
            durability=self._durability,
        )
        return VerifyReport(
            ok=True,
            entries=len(entries),
            full=True,
            suffix_entries=len(entries),
            tip_hash=tip,
        )

    def _entries_from_bytes(self, data: bytes) -> list[LedgerEntry]:
        entries: list[LedgerEntry] = []
        for line in data.splitlines():
            if not line.strip():
                continue
            self.lines_parsed += 1
            try:
                entries.append(LedgerEntry.model_validate_json(line))
            except Exception as exc:
                raise LedgerVerificationError(f"ledger line does not parse: {exc}") from exc
        return entries

    def _verify_chain(
        self,
        entries: list[LedgerEntry],
        *,
        start_seq: int,
        prev_hash: str,
    ) -> None:
        for offset, entry in enumerate(entries):
            expected = start_seq + offset
            if entry.seq != expected:
                raise LedgerVerificationError(
                    f"entry at position {expected} has seq={entry.seq}, expected {expected}"
                )
            if entry.prev_hash != prev_hash:
                raise LedgerVerificationError(
                    f"entry seq={entry.seq} has prev_hash={entry.prev_hash!r}, "
                    f"expected {prev_hash!r} (chain broken)"
                )
            recomputed = compute_entry_hash(entry.model_copy(update={"entry_hash": "pending"}))
            if recomputed != entry.entry_hash:
                raise LedgerVerificationError(
                    f"entry seq={entry.seq} entry_hash={entry.entry_hash!r} does not match "
                    f"recomputed {recomputed!r} (tampered or corrupted)"
                )
            if entry.action == "close_receipt":
                _validate_close_receipt_evidence(entry)
            prev_hash = entry.entry_hash

    def _check_signatures(self, entries: list[LedgerEntry]) -> None:
        from recertia.ledger.signer import SIG_FIELD, ledger_hmac_key, signature_ok

        key = ledger_hmac_key()
        seen_cutover = False
        for entry in entries:
            sig = entry.evidence.get(SIG_FIELD)
            if not seen_cutover and sig is None:
                continue
            seen_cutover = True
            if key is None:
                raise LedgerVerificationError("ledger signature key is not configured")
            if not signature_ok(entry, key):
                raise LedgerVerificationError(
                    f"entry seq={entry.seq} ledger signature does not verify"
                )

    def repair_torn_tail(self, *, actor: str, dry_run: bool = False) -> RepairReport:
        from recertia.ledger.repair import repair_locked

        with self._held():
            return repair_locked(self, actor=actor, dry_run=dry_run)


def _validate_close_receipt_evidence(entry: LedgerEntry) -> None:
    """``close_receipt`` evidence must be a StoreReceipt (HMAC sig field stripped)."""

    from contracts.receipt import StoreReceipt

    evidence = {key: value for key, value in entry.evidence.items() if key != "_ledger_sig"}
    try:
        StoreReceipt.model_validate(evidence)
    except Exception as exc:
        raise LedgerVerificationError(
            f"entry seq={entry.seq} close_receipt evidence is not a StoreReceipt: {exc}"
        ) from exc


def _resolve_durability(
    durability: str | Durability | None,
    lock_timeout_s: float | None,
) -> tuple[str, float]:
    if isinstance(durability, Durability):
        name = "fast" if not durability.fsync_file else "normal"
        return name, 5.0 if lock_timeout_s is None else lock_timeout_s
    if durability is None or lock_timeout_s is None:
        from recertia.settings import load_settings

        settings = load_settings()
        if durability is None:
            durability = settings.durability
        if lock_timeout_s is None:
            lock_timeout_s = settings.ledger_lock_timeout_s
    return str(durability), float(lock_timeout_s)


def _settings_hook() -> LedgerHook | None:
    from recertia.ledger.signer import hmac_hook_from_settings

    return hmac_hook_from_settings()
