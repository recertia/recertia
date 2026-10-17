"""Hash-chain mechanics for the provenance ledger (specs §21).

Storage is an append-only JSONL file: one ``LedgerEntry`` per line, in ``seq`` order. This is
the M0-minimal persistence — durable across process restarts, trivially diffable, and exactly
as much mechanism as ``recertia ledger verify`` needs. A SQLite/Postgres-backed store (per the
tech stack in ``docs/archive/2026-Q3/implementation-plan.md``) can replace this later without changing
``LedgerEntry`` itself, since the contract lives in ``contracts/ledger.py``, not here.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from contracts.ledger import LedgerAction, LedgerEntry

GENESIS_HASH = "0" * 64


class LedgerVerificationError(Exception):
    """The chain does not verify: a hash mismatch, a broken link, or a gap in ``seq``."""


def _canonical_bytes(entry: LedgerEntry) -> bytes:
    """Canonical serialisation of every field except ``entry_hash`` (specs §21)."""

    payload = entry.model_dump(mode="json", exclude={"entry_hash"})
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_entry_hash(entry: LedgerEntry) -> str:
    return hashlib.sha256(_canonical_bytes(entry)).hexdigest()


@contextmanager
def _cross_process_lock(path: Path) -> Iterator[None]:
    """Exclusive ``flock`` beside ``path`` (same pattern as the skill version lock).

    Held across tip-read, append, and fsync so two processes cannot fork the chain.
    The in-process ``threading.Lock`` is taken by the caller first.
    """

    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if sys.platform == "win32":
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if sys.platform == "win32":
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class HashChainLedger:
    """One ledger, backed by one append-only JSONL file.

    Thread-safe within a process via a lock, and cross-process safe via ``flock`` held
    across tip-read, append, and fsync. ``verify`` recomputes the whole chain from disk
    rather than trusting in-memory state. The hash is still SHA-256 of the canonical
    payload.

    ``append`` needs only the current tip ``(seq, entry_hash)``, so both are cached after
    the first scan. The cache is validated against the file's ``(size, mtime_ns)`` before
    every use: an external append or rewrite changes the stat and forces one rescan, which
    keeps the fast path correct without re-parsing history on every write.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._tip: tuple[int, str] | None = None  # (next_seq, tip_hash)
        self._stat: tuple[int, int] | None = None  # (size, mtime_ns) when _tip was computed

    @property
    def path(self) -> Path:
        return self._path

    def _file_stat(self) -> tuple[int, int] | None:
        try:
            st = self._path.stat()
        except FileNotFoundError:
            return None
        return (st.st_size, st.st_mtime_ns)

    def _tip_unlocked(self) -> tuple[int, str]:
        """Current ``(next_seq, tip_hash)``; rescans only when the file changed on disk."""

        if self._tip is not None and self._stat == self._file_stat():
            return self._tip
        entries = self._read_all()
        tip = (len(entries), entries[-1].entry_hash if entries else GENESIS_HASH)
        self._tip = tip
        self._stat = self._file_stat()
        return tip

    def _read_all(self) -> list[LedgerEntry]:
        if not self._path.exists():
            return []
        entries: list[LedgerEntry] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(LedgerEntry.model_validate_json(line))
        return entries

    def tip_hash(self) -> str:
        with self._lock:
            return self._tip_unlocked()[1]

    def append(
        self,
        *,
        actor: str,
        action: LedgerAction,
        target: str,
        evidence: dict | None = None,
        at: datetime | None = None,
    ) -> LedgerEntry:
        """Append one entry. Returns the entry as written, with ``seq`` and hashes filled in."""

        with self._lock, _cross_process_lock(self._path):
            seq, prev_hash = self._tip_unlocked()
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
            entry = draft.model_copy(update={"entry_hash": compute_entry_hash(draft)})
            line = entry.model_dump_json() + "\n"
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            self._tip = (seq + 1, entry.entry_hash)
            self._stat = self._file_stat()
            return entry

    def entries(self) -> list[LedgerEntry]:
        return self._read_all()

    def verify(self) -> None:
        """Recompute the whole chain; raise :class:`LedgerVerificationError` on the first break.

        Mirrors ``GET /v1/ledger/verify`` (specs §21): every entry's ``entry_hash`` must
        recompute correctly, every ``seq`` must be contiguous from 0, and every ``prev_hash``
        must equal the predecessor's ``entry_hash``.
        """

        entries = self._read_all()
        with self._lock:
            self._tip = (len(entries), entries[-1].entry_hash if entries else GENESIS_HASH)
            self._stat = self._file_stat()
        prev_hash = GENESIS_HASH
        for i, entry in enumerate(entries):
            if entry.seq != i:
                raise LedgerVerificationError(f"entry at position {i} has seq={entry.seq}, expected {i}")
            if entry.prev_hash != prev_hash:
                raise LedgerVerificationError(
                    f"entry seq={entry.seq} has prev_hash={entry.prev_hash!r}, "
                    f"expected {prev_hash!r} (chain broken)"
                )
            draft = entry.model_copy(update={"entry_hash": "pending"})
            recomputed = compute_entry_hash(draft)
            if recomputed != entry.entry_hash:
                raise LedgerVerificationError(
                    f"entry seq={entry.seq} entry_hash={entry.entry_hash!r} does not match "
                    f"recomputed {recomputed!r} (tampered or corrupted)"
                )
            prev_hash = entry.entry_hash
