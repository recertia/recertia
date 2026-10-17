"""Two processes cannot drain the same revoke prefix twice (F33)."""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

from recertia.memory.procedural.lineage import RevokeQueue


def _drain_to(path: str, limit: int, dest: str) -> None:
    items = RevokeQueue(path).drain(limit)
    Path(dest).write_text(json.dumps(items), encoding="utf-8")


def test_two_process_drain_no_loss_no_dup(tmp_path: Path) -> None:
    path = tmp_path / "revoke.jsonl"
    queue = RevokeQueue(path)
    expected = [f"s{index}" for index in range(80)]
    for source_id in expected:
        queue.enqueue(source_kind="skill", source_id=source_id, reason="q")

    out_a = tmp_path / "a.json"
    out_b = tmp_path / "b.json"
    ctx = multiprocessing.get_context("spawn")
    first = ctx.Process(target=_drain_to, args=(str(path), 80, str(out_a)))
    second = ctx.Process(target=_drain_to, args=(str(path), 80, str(out_b)))
    first.start()
    second.start()
    first.join(30)
    second.join(30)
    assert first.exitcode == 0
    assert second.exitcode == 0
    got_a = json.loads(out_a.read_text(encoding="utf-8"))
    got_b = json.loads(out_b.read_text(encoding="utf-8"))
    ids = [item["source_id"] for item in got_a] + [item["source_id"] for item in got_b]
    assert sorted(ids) == sorted(expected)
    assert len(ids) == len(set(ids))
    assert queue.drain(limit=10) == []
