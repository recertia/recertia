"""Run list pages use checkpoint summaries, not a full state parse per hop."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from contracts.run import RunState, Task
from recertia.app.runs import RunService
from recertia.errors import RecertiaError
from recertia.graph.store import CheckpointStore
from recertia.runtime.cursor import RunPosition, encode_run_cursor
from recertia.runtime.registry import RunRegistry


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


T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)


def _at(seconds: int, micros: int = 0) -> datetime:
    return T0 + timedelta(seconds=seconds, microseconds=micros)


def _text(when: datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M:%S")


def _seed_summaries(root: Path, rows: Iterable[tuple[str, datetime, str, str | None]]) -> None:
    """Write checkpoint summaries ``(run_id, created_at, task_class, terminal)``."""

    root.mkdir(parents=True, exist_ok=True)
    CheckpointStore(root / "checkpoints.db").close()
    conn = sqlite3.connect(root / "checkpoints.db")
    conn.executemany(
        "INSERT INTO run_summaries (run_id, status, terminal, updated_at, cost_usd, task_class,"
        " attempt_no, arm, created_at) VALUES (?, ?, ?, ?, 0, ?, 1, 'treatment', ?)",
        [
            (
                run_id,
                terminal or "running",
                terminal,
                _text(when),
                task_class,
                _text(when),
            )
            for run_id, when, task_class, terminal in rows
        ],
    )
    conn.commit()
    conn.close()


def _seed_registry(root: Path, tenant_id: str, rows: Iterable[tuple[str, str]]) -> None:
    """Reserve runs ``(run_id, created_at_text)`` the way the API does, then set their ``created_at``."""

    root.mkdir(parents=True, exist_ok=True)
    registry = RunRegistry(root / "runtime.db")
    for run_id, created_at in rows:
        registry.reserve(tenant_id, run_id, request_hash=f"h-{run_id}")
        registry._conn.execute(
            "UPDATE runs SET created_at = ? WHERE tenant_id = ? AND run_id = ?",
            (created_at, tenant_id, run_id),
        )
    registry._conn.commit()
    registry.close()


def _iso(when: datetime) -> str:
    return when.isoformat()


def _rec(run_id: str, created_at: datetime | None, **fields: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "run_id": run_id,
        "task_class": "repo-chore",
        "terminal": None,
        "status": "running",
        "attempt_no": 1,
        "created_at": created_at,
        "cost_usd": 0.0,
        "arm": "treatment",
        "tenant_id": "tenant-a",
    }
    values.update(fields)
    return SimpleNamespace(**values)


def _service(*records: SimpleNamespace) -> RunService:
    runs: dict[tuple[str, str], Any] = {(rec.tenant_id, rec.run_id): rec for rec in records}
    return RunService(_Quota(), runs, lambda _tenant, _run: None)


def _walk(service: RunService, root: Path, limit: int, **filters: Any) -> list[str]:
    """Follow ``next_cursor`` to the end. Returns the run ids in the order served."""

    seen: list[str] = []
    cursor = None
    for _ in range(1000):
        page = service.list("tenant-a", runs_root=root, limit=limit, cursor=cursor, **filters)
        assert len(page["items"]) <= limit
        seen += [item["run_id"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            return seen
    raise AssertionError("cursor walk did not end")


def test_summary_page_is_newest_first_with_run_id_tie_break(tmp_path: Path) -> None:
    _seed_summaries(
        tmp_path,
        [
            ("a", _at(0), "repo-chore", None),
            ("b", _at(5), "repo-chore", None),
            ("c", _at(5), "repo-chore", None),
            ("d", _at(9), "repo-chore", None),
            ("e", _at(5), "repo-chore", None),
        ],
    )
    store = CheckpointStore(tmp_path / "checkpoints.db")
    first = store.list_summaries_page(limit=3)
    assert [row["run_id"] for row in first] == ["d", "e", "c"]
    after = store.list_summaries_page(limit=3, created_at_before=_text(_at(5)), run_id_before="c")
    assert [row["run_id"] for row in after] == ["b", "a"]
    store.close()


def test_summary_page_filters_apply_with_the_keyset(tmp_path: Path) -> None:
    _seed_summaries(
        tmp_path,
        [
            ("a", _at(1), "bug", "solved"),
            ("b", _at(2), "repo-chore", "solved"),
            ("c", _at(3), "bug", "unsolved"),
            ("d", _at(4), "bug", "solved"),
            ("e", _at(5), "bug", None),
        ],
    )
    store = CheckpointStore(tmp_path / "checkpoints.db")
    rows = store.list_summaries_page(limit=10, task_class="bug", terminal="solved")
    assert [row["run_id"] for row in rows] == ["d", "a"]
    older = store.list_summaries_page(
        limit=10, created_at_before=_text(_at(4)), run_id_before="d", task_class="bug"
    )
    assert [row["run_id"] for row in older] == ["c", "a"]
    assert store.list_summaries_page(limit=0) == []
    store.close()


def test_summary_page_is_served_by_the_created_index(tmp_path: Path) -> None:
    _seed_summaries(tmp_path, [("a", _at(1), "bug", None)])
    store = CheckpointStore(tmp_path / "checkpoints.db")
    plan = store._conn.execute(
        "EXPLAIN QUERY PLAN SELECT run_id FROM run_summaries WHERE created_at < ? "
        "ORDER BY created_at DESC, run_id DESC LIMIT 5",
        ("2030-01-01 00:00:00",),
    ).fetchall()
    detail = " ".join(str(row[-1]) for row in plan)
    assert "run_summaries_created" in detail
    assert "TEMP B-TREE" not in detail
    store.close()


def test_run_service_list_is_keyset_newest_first(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _seed_summaries(
        root,
        [
            ("a", _at(1), "repo-chore", "solved"),
            ("c", _at(3), "repo-chore", "solved"),
            ("e", _at(5), "repo-chore", "solved"),
        ],
    )
    service = _service(
        _rec("b", _at(2), terminal="solved", status="solved"),
        _rec("z", _at(9), tenant_id="tenant-b"),
    )
    first = service.list("tenant-a", runs_root=root, limit=2)
    assert [item["run_id"] for item in first["items"]] == ["e", "c"]
    assert first["next_cursor"] is not None
    second = service.list("tenant-a", runs_root=root, limit=2, cursor=first["next_cursor"])
    assert [item["run_id"] for item in second["items"]] == ["b", "a"]
    assert second["next_cursor"] is None
    assert all(item["tenant_id"] == "tenant-a" for item in first["items"] + second["items"])


def test_equal_created_at_orders_by_run_id_descending_across_pages(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runs"
    _seed_summaries(root, [(name, _at(7), "repo-chore", None) for name in "abcde"])
    service = _service()
    assert _walk(service, root, 2) == ["e", "d", "c", "b", "a"]
    assert _walk(service, root, 1) == ["e", "d", "c", "b", "a"]


@pytest.mark.parametrize("total", [1, 2, 4, 5])
def test_exact_fit_last_page_has_no_next_cursor(tmp_path: Path, total: int) -> None:
    root = tmp_path / "runs"
    _seed_summaries(root, [(f"r{i}", _at(i), "repo-chore", None) for i in range(total)])
    service = _service()
    page = service.list("tenant-a", runs_root=root, limit=total)
    assert len(page["items"]) == total
    assert page["next_cursor"] is None
    short = service.list("tenant-a", runs_root=root, limit=total + 1)
    assert short["next_cursor"] is None
    if total > 1:
        partial = service.list("tenant-a", runs_root=root, limit=total - 1)
        assert partial["next_cursor"] is not None


def test_empty_listing_has_no_cursor(tmp_path: Path) -> None:
    page = _service().list("tenant-a", runs_root=tmp_path / "none", limit=5)
    assert page == {"items": [], "next_cursor": None}


def test_registry_only_run_lists_as_accepted_and_checkpoint_state_wins(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runs"
    _seed_registry(
        root,
        "tenant-a",
        [
            ("only-reg", _iso(_at(30))),
            ("both", _iso(_at(20))),
            ("only-cp", _iso(_at(10))),
        ],
    )
    _seed_summaries(
        root,
        [
            ("both", _at(21), "repo-chore", "solved"),
            ("only-cp", _at(10), "bug", "unsolved"),
        ],
    )
    items = _service().list("tenant-a", runs_root=root, limit=10)["items"]
    assert [item["run_id"] for item in items] == ["only-reg", "both", "only-cp"]
    by_id = {item["run_id"]: item for item in items}
    assert by_id["only-reg"]["status"] == "accepted"
    assert by_id["both"]["status"] == "solved"
    assert by_id["both"]["terminal"] == "solved"
    assert by_id["only-cp"]["task_class"] == "bug"
    assert len({item["run_id"] for item in items}) == 3


def test_filters_apply_to_registry_rows(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _seed_registry(root, "tenant-a", [("acc", _iso(_at(30))), ("fin", _iso(_at(20)))])
    _seed_summaries(
        root,
        [
            ("fin", _at(20), "bug", "solved"),
            ("other", _at(10), "repo-chore", "unsolved"),
        ],
    )
    service = _service()
    assert _walk(service, root, 10, terminal="solved") == ["fin"]
    assert _walk(service, root, 10, task_class="bug") == ["fin"]
    assert _walk(service, root, 10, task_class="repo-chore") == ["other"]
    assert _walk(service, root, 10, terminal="unsolved") == ["other"]
    assert _walk(service, root, 10) == ["acc", "fin", "other"]


def test_registry_rows_are_scoped_to_the_tenant(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _seed_registry(root, "tenant-a", [("mine", _iso(_at(1)))])
    _seed_registry(root, "tenant-b", [("theirs", _iso(_at(2)))])
    service = _service()
    assert _walk(service, root, 10) == ["mine"]
    other = service.list("tenant-b", runs_root=root, limit=10)
    assert [item["run_id"] for item in other["items"]] == ["theirs"]


def test_registry_keyset_pages_without_loss_or_repeat(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    rows = [(f"r{i}", _iso(_at(i // 3))) for i in range(10)]
    _seed_registry(root, "tenant-a", rows)
    expected = sorted(rows, key=lambda row: (row[1], row[0]), reverse=True)
    assert _walk(_service(), root, 3) == [run_id for run_id, _ in expected]
    assert _walk(_service(), root, 1) == [run_id for run_id, _ in expected]


def test_undated_runs_sort_last_and_page_through(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _seed_registry(root, "tenant-a", [("u1", ""), ("u2", ""), ("d1", _iso(_at(1)))])
    _seed_summaries(root, [("d2", _at(2), "repo-chore", None)])
    service = _service(_rec("mem-undated", None), _rec("mem-dated", _at(3)))
    expected = ["mem-dated", "d2", "d1", "u2", "u1", "mem-undated"]
    assert _walk(service, root, 2) == expected
    assert _walk(service, root, 1) == expected
    assert _walk(service, root, 10) == expected


def test_in_memory_run_keeps_its_stored_position(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _seed_summaries(
        root,
        [
            ("x", _at(10), "repo-chore", None),
            ("m", _at(5), "repo-chore", None),
            ("y", _at(1), "repo-chore", None),
        ],
    )
    service = _service(_rec("m", _at(99), terminal="solved", status="solved"))
    pages = [_walk(service, root, size) for size in (1, 2, 3)]
    assert pages == [["x", "m", "y"]] * 3
    items = service.list("tenant-a", runs_root=root, limit=10)["items"]
    assert items[1]["status"] == "solved"


def test_sub_second_position_does_not_skip_rows_of_its_second(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _seed_summaries(root, [("a", _at(5), "repo-chore", None), ("z", _at(5), "repo-chore", None)])
    service = _service(_rec("m", _at(5, 500_000)))
    assert _walk(service, root, 1) == ["m", "z", "a"]


def test_filters_apply_to_in_memory_runs(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    service = _service(
        _rec("a", _at(1), task_class="bug", terminal="solved", status="solved"),
        _rec("b", _at(2), task_class="repo-chore"),
    )
    assert _walk(service, root, 10, task_class="bug") == ["a"]
    assert _walk(service, root, 10, terminal="solved") == ["a"]
    assert _walk(service, root, 10) == ["b", "a"]


def test_list_walks_10050_runs_without_loss_or_repeat(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    checkpoint_rows = [(f"c{i:05d}", _at(i // 4), "repo-chore", None) for i in range(10_050)]
    _seed_summaries(root, checkpoint_rows)
    registry_rows = [(f"g{i:03d}", _iso(_at(i * 97, 250_000))) for i in range(40)]
    _seed_registry(root, "tenant-a", registry_rows)
    memory = [_rec(f"m{i:03d}", _at(i * 211 + 3)) for i in range(10)]
    memory.append(_rec("m-undated", None))
    expected_keys = (
        [(when, run_id) for run_id, when, _, _ in checkpoint_rows]
        + [(datetime.fromisoformat(text), run_id) for run_id, text in registry_rows]
        + [(rec.created_at, rec.run_id) for rec in memory if rec.created_at is not None]
    )
    expected = [run_id for _, run_id in sorted(expected_keys, reverse=True)] + ["m-undated"]
    seen = _walk(_service(*memory), root, 100)
    assert len(seen) == 10_050 + 40 + 11
    assert len(set(seen)) == len(seen)
    assert seen == expected


def test_page_reads_only_a_limit_sized_slice(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _seed_summaries(root, [(f"c{i:05d}", _at(i), "repo-chore", None) for i in range(3000)])
    seen_limits: list[int] = []
    original = CheckpointStore.list_summaries_page

    def spy(self: CheckpointStore, **kwargs: Any) -> list[dict[str, object]]:
        seen_limits.append(kwargs["limit"])
        return original(self, **kwargs)

    CheckpointStore.list_summaries_page = spy  # type: ignore[method-assign]
    try:
        page = _service().list("tenant-a", runs_root=root, limit=5)
    finally:
        CheckpointStore.list_summaries_page = original  # type: ignore[method-assign]
    assert len(page["items"]) == 5
    assert seen_limits == [6]


def test_malformed_cursor_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RecertiaError) as caught:
        _service().list("tenant-a", runs_root=tmp_path, limit=5, cursor="not-a-cursor")
    assert caught.value.http_status == 400
    ok = encode_run_cursor(RunPosition(_at(1), "r1"))
    assert _service().list("tenant-a", runs_root=tmp_path, limit=5, cursor=ok)["items"] == []
