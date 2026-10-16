"""Concurrent ledger appends keep one contiguous chain."""

from __future__ import annotations

import multiprocessing
import threading
from pathlib import Path

from recertia.ledger import HashChainLedger


def _append_many(path: str, count: int, actor: str) -> None:
    ledger = HashChainLedger(path)
    for index in range(count):
        ledger.append(actor=actor, action="write", target=f"{actor}-{index}")


def test_parallel_appends_two_instances_keep_chain_valid(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    errors: list[BaseException] = []

    def worker(actor: str) -> None:
        try:
            _append_many(str(path), 200, actor)
        except BaseException as exc:  # noqa: BLE001 — surface worker failures
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert errors == []
    ledger = HashChainLedger(path)
    report = ledger.verify(full=True)
    assert report.ok
    assert report.entries == 400
    assert [entry.seq for entry in ledger.entries()] == list(range(400))


def test_parallel_appends_four_processes_keep_chain_valid(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    procs = [
        multiprocessing.Process(target=_append_many, args=(str(path), 40, f"p{i}"))
        for i in range(4)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
        assert proc.exitcode == 0
    text = path.read_text(encoding="utf-8")
    assert "entry at position" not in text
    ledger = HashChainLedger(path)
    report = ledger.verify(full=True)
    assert report.entries == 160
    assert [entry.seq for entry in ledger.entries()] == list(range(160))
