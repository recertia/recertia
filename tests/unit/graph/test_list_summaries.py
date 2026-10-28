"""Run list pages use checkpoint summaries, not a full state parse per hop."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.run import RunState, Task
from recertia.app.runs import RunService
from recertia.graph.store import CheckpointStore


def _state(run_id: str, *, task_class: str = "repo-chore", terminal: str | None = None) -> RunState:
    return RunState(
        run_id=run_id,
        task=Task(
            task_id=run_id,
            request="x",
            task_class=task_class,
            submitted_at=datetime.now(timezone.utc),
        ),
        terminal=terminal,  # type: ignore[arg-type]
        attempt_no=2,
    )


class _Quota:
    def admit(self, tenant_id: str, *, quota: object = None) -> None:
        del tenant_id, quota

    def release_inflight(self, tenant_id: str) -> None:
        del tenant_id

    def complete(self, tenant_id: str, *, cost_usd: float = 0.0) -> None:
        del tenant_id, cost_usd


def test_list_summaries_reads_latest_hop_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = CheckpointStore(tmp_path / "checkpoints.db")
    store.save("a", 0, "intake", "solve", _state("a", terminal=None))
    store.save("a", 1, "solve", None, _state("a", terminal="solved"))
    store.save("b", 0, "intake", None, _state("b", task_class="bug", terminal="unsolved"))
    store.save("c", 0, "intake", None, _state("c", terminal="solved"))

    def _boom(cls: object, raw: object) -> object:
        del cls, raw
        raise AssertionError("list must not parse checkpoint state")

    monkeypatch.setattr(RunState, "model_validate_json", classmethod(_boom))
    page = store.list_summaries(limit=2)
    assert [row["run_id"] for row in page] == ["a", "b"]
    assert page[0]["terminal"] == "solved"
    assert page[0]["status"] == "solved"
    assert page[0]["attempt_no"] == 2
    store._conn.execute("UPDATE checkpoints SET state_json = '{}'")
    store._conn.commit()
    again = store.list_summaries(limit=2)
    assert [row["terminal"] for row in again] == ["solved", "unsolved"]
    filtered = store.list_summaries(limit=10, cursor="a", terminal="solved")
    assert [row["run_id"] for row in filtered] == ["c"]
    store.close()


def test_run_service_list_is_keyset(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    store = CheckpointStore(root / "checkpoints.db")
    for run_id in ("a", "c", "e"):
        store.save(run_id, 0, "intake", None, _state(run_id, terminal="solved"))
    store.close()

    class _Rec:
        def __init__(self, run_id: str) -> None:
            self.run_id = run_id
            self.task_class = "repo-chore"
            self.terminal = "solved"
            self.status = "solved"
            self.attempt_no = 1
            self.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self.cost_usd = 0.0
            self.arm = "treatment"
            self.tenant_id = "tenant-a"

    runs = {("tenant-a", "b"): _Rec("b"), ("tenant-b", "z"): _Rec("z")}
    service = RunService(_Quota(), runs, lambda _tenant, _run: None)
    first = service.list("tenant-a", runs_root=root, limit=2)
    assert [item["run_id"] for item in first["items"]] == ["a", "b"]
    assert first["next_cursor"] == "b"
    second = service.list("tenant-a", runs_root=root, limit=2, cursor=first["next_cursor"])
    assert [item["run_id"] for item in second["items"]] == ["c", "e"]
    assert second["next_cursor"] is None
    assert all(item["tenant_id"] == "tenant-a" for item in second["items"])


def test_list_cursor_is_opaque_and_rejects_tampered(tmp_path: Path) -> None:
    """REC-056f: cursor is base64 JSON; a tampered cursor raises InvalidCursor."""
    from recertia.runtime.cursor import decode_cursor, encode_cursor
    from recertia.runtime.errors import InvalidCursor

    encoded = encode_cursor("2026-10-29T00:00:00+00:00", "run-1")
    created, rid = decode_cursor(encoded)
    assert (created, rid) == ("2026-10-29T00:00:00+00:00", "run-1")
    try:
        decode_cursor("not-a-cursor")
        raise AssertionError("should have raised")
    except InvalidCursor:
        pass
