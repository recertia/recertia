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
    from recertia.runtime.registry import RunRegistry

    root = tmp_path / "runs"
    registry = RunRegistry(root / "runtime.db")
    stamps = {
        "a": "2026-01-01T00:00:03+00:00",
        "b": "2026-01-01T00:00:02+00:00",
        "c": "2026-01-01T00:00:01+00:00",
    }
    try:
        for run_id, created in stamps.items():
            registry._conn.execute(
                """
                INSERT INTO runs(
                    tenant_id, run_id, status, request_hash, summary_json,
                    workdir, created_at, updated_at
                ) VALUES (?, ?, 'solved', '', ?, NULL, ?, ?)
                """,
                (
                    "tenant-a",
                    run_id,
                    (
                        '{"run_id":"%s","task_class":"repo-chore","terminal":"solved",'
                        '"status":"solved","tenant_id":"tenant-a","created_at":"%s"}'
                    )
                    % (run_id, created),
                    created,
                    created,
                ),
            )
        registry._conn.commit()
    finally:
        registry.close()

    class _Rec:
        def __init__(self, run_id: str, created: str) -> None:
            self.run_id = run_id
            self.task_class = "repo-chore"
            self.terminal = "solved"
            self.status = "solved"
            self.attempt_no = 1
            self.created_at = created
            self.cost_usd = 0.0
            self.arm = "treatment"
            self.tenant_id = "tenant-a"

    runs = {
        ("tenant-a", "mem"): _Rec("mem", "2026-01-01T00:00:00+00:00"),
        ("tenant-b", "z"): _Rec("z", "2026-01-01T00:00:04+00:00"),
    }
    service = RunService(_Quota(), runs, lambda _tenant, _run: None)
    first = service.list("tenant-a", runs_root=root, limit=2)
    assert [item["run_id"] for item in first["items"]] == ["a", "b"]
    assert first["next_cursor"]
    second = service.list("tenant-a", runs_root=root, limit=2, cursor=first["next_cursor"])
    assert [item["run_id"] for item in second["items"]] == ["c", "mem"]
    assert second["next_cursor"] is None
    assert all(item["tenant_id"] == "tenant-a" for item in second["items"])
