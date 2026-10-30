"""REC-056-nits C3: a SQLite error from the affordance store is logged, not raised.

Affordances are advisory history. ``record_new_affordances`` (run at the end of every
attempt) and ``classify_failure``'s tool lookup catch ``sqlite3.Error`` only.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from contracts.budget import Budget, Spend
from contracts.failure import FailureSignal
from contracts.run import RunState, Task
from recertia.memory.affordance import AffordanceStore
from recertia.nodes.attempt import record_new_affordances
from recertia.nodes.classify_failure import classify_failure
from recertia.nodes.context import NodeContext
from recertia.solver.tools import ToolResult

_NOW = datetime.fromisoformat("2026-10-30T00:00:00+00:00")


class _Window:
    def __init__(self, invocations: list[ToolResult]) -> None:
        self._invocations = invocations

    def new_invocations(self) -> list[ToolResult]:
        return self._invocations

    def new_conflicts(self) -> list[Any]:
        return []


def _ctx(tmp_path: Path, affordances: Any) -> NodeContext:
    unused: Any = None
    return NodeContext(
        run_id="r",
        attempt_no=1,
        node="classify_failure",
        workdir=tmp_path,
        workspaces=unused,
        ledger=unused,
        ops=unused,
        affordances=affordances,
    )


def _locked(*_args: Any, **_kwargs: Any) -> Any:
    raise sqlite3.OperationalError("database is locked")


def test_locked_record_tool_does_not_fail_the_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = AffordanceStore(tmp_path / "affordances.json")
    try:
        monkeypatch.setattr(store, "record_tool", _locked)
        window: Any = _Window([ToolResult(tool="t", ok=True, duration_s=1.0)])
        with caplog.at_level(logging.WARNING, logger="recertia.nodes.attempt"):
            record_new_affordances(_ctx(tmp_path, store), window)
    finally:
        store.close()
    assert "database is locked" in caplog.text


def test_record_new_affordances_does_not_swallow_other_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AffordanceStore(tmp_path / "affordances.json")

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("not a database error")

    try:
        monkeypatch.setattr(store, "record_tool", broken)
        window: Any = _Window([ToolResult(tool="t", ok=True, duration_s=1.0)])
        with pytest.raises(RuntimeError, match="^not a database error$"):
            record_new_affordances(_ctx(tmp_path, store), window)
    finally:
        store.close()


class _LockedAffordances:
    @property
    def tools(self) -> dict[str, Any]:
        raise sqlite3.OperationalError("database is locked")


def _failed_state(detail: str) -> RunState:
    return RunState(
        run_id="r",
        task=Task(task_id="t", request="x", submitted_at=_NOW),
        failure_signal=FailureSignal(source="solver", detail=detail, at=_NOW),
        spent=Spend(attempts=1),
        budget=Budget(max_attempts=4),
    )


def test_locked_affordance_lookup_does_not_fail_classify_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="recertia.nodes.classify_failure"):
        outcome = classify_failure(
            _failed_state("tool=net: connection reset"), _ctx(tmp_path, _LockedAffordances())
        )
    assert outcome.state.failure is not None
    assert outcome.state.failure.failure_class == "tool"
    assert "database is locked" in caplog.text
