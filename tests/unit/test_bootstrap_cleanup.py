"""``build_default_orchestrator`` closes what it opened when wiring fails (REC-096).

The skill index opens a sqlite connection before the run persistence is opened. A
failure between the two (the persistence open, an index rebuild) used to leave that
connection for the garbage collector. Spies on ``close`` count the calls, so the
tests do not depend on when garbage collection runs (a module-wide unraisable-warning
filter would also catch connections that earlier tests left for the collector).
"""

from __future__ import annotations

import errno
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

import recertia.graph.engine as engine_mod
import recertia.graph.persistence as persistence_mod
from recertia.app.authz import PolicyDecisionPoint
from recertia.bootstrap import build_default_orchestrator
from recertia.memory.procedural.lineage import LineageServices
from recertia.retrieval.index import SkillIndex


@pytest.fixture
def index_closes(monkeypatch: pytest.MonkeyPatch) -> Counter[int]:
    """Count ``SkillIndex.close`` calls per instance; the real close still runs."""

    closes: Counter[int] = Counter()
    real_close = SkillIndex.close

    def spy(self: SkillIndex) -> None:
        closes[id(self)] += 1
        real_close(self)

    monkeypatch.setattr(SkillIndex, "close", spy)
    return closes


@pytest.fixture
def opened_indexes(monkeypatch: pytest.MonkeyPatch) -> list[SkillIndex]:
    opened: list[SkillIndex] = []
    real_init = SkillIndex.__init__

    def spy(self: SkillIndex, *args: Any, **kwargs: Any) -> None:
        real_init(self, *args, **kwargs)
        opened.append(self)

    monkeypatch.setattr(SkillIndex, "__init__", spy)
    return opened


def _build(tmp_path: Path) -> Any:
    return build_default_orchestrator(
        tmp_path / "runs",
        skills_root=tmp_path / "skills",
        facts_root=tmp_path / "facts",
    )


def _assert_closed(index: SkillIndex) -> None:
    with pytest.raises(sqlite3.ProgrammingError):
        index._conn.execute("SELECT 1")


def test_lineage_and_policy_decision_point_hold_nothing_to_close() -> None:
    """The cleanup stack registers only the skill index. If either of these gains a
    ``close``, register it in ``build_default_orchestrator`` and update this test."""

    assert not hasattr(LineageServices, "close")
    assert not hasattr(PolicyDecisionPoint, "close")


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.OperationalError("database is locked"),
        OSError(errno.EIO, "I/O error"),
    ],
    ids=["sqlite3_error", "os_error"],
)
def test_failed_run_persistence_open_closes_the_skill_index_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    index_closes: Counter[int],
    opened_indexes: list[SkillIndex],
    error: Exception,
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(persistence_mod, "open_run_persistence", fail)
    with pytest.raises(type(error)) as caught:
        _build(tmp_path)
    assert caught.value is error
    assert len(opened_indexes) == 1
    assert index_closes == Counter({id(opened_indexes[0]): 1})
    _assert_closed(opened_indexes[0])


def test_failed_index_rebuild_closes_the_skill_index_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    index_closes: Counter[int],
    opened_indexes: list[SkillIndex],
) -> None:
    persistence_opens: list[object] = []

    def boom(self: SkillIndex, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("rebuild failed")

    def spy_open(*args: Any, **kwargs: Any) -> Any:
        persistence_opens.append(args)
        raise AssertionError("the persistence open must not be reached")

    monkeypatch.setattr(SkillIndex, "is_fresh", lambda self, _fp: False)
    monkeypatch.setattr(SkillIndex, "rebuild", boom)
    monkeypatch.setattr(persistence_mod, "open_run_persistence", spy_open)
    with pytest.raises(RuntimeError, match="rebuild failed"):
        _build(tmp_path)
    assert persistence_opens == []
    assert len(opened_indexes) == 1
    assert index_closes == Counter({id(opened_indexes[0]): 1})
    _assert_closed(opened_indexes[0])


def test_failed_wiring_after_the_persistence_open_closes_each_handle_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    index_closes: Counter[int],
    opened_indexes: list[SkillIndex],
) -> None:
    """The persistence handle keeps its own cleanup; the stack must not close it again."""

    persistence_closes: list[object] = []
    real_open = persistence_mod.open_run_persistence

    def spy_open(*args: Any, **kwargs: Any) -> Any:
        handle = real_open(*args, **kwargs)
        real_close = handle.close

        def close() -> None:
            persistence_closes.append(handle)
            real_close()

        handle.close = close  # type: ignore[method-assign]
        return handle

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("orchestrator wiring failed")

    monkeypatch.setattr(persistence_mod, "open_run_persistence", spy_open)
    monkeypatch.setattr(engine_mod, "GraphOrchestrator", fail)
    with pytest.raises(RuntimeError, match="orchestrator wiring failed"):
        _build(tmp_path)
    assert len(persistence_closes) == 1
    assert len(opened_indexes) == 1
    assert index_closes == Counter({id(opened_indexes[0]): 1})


def test_successful_build_hands_the_open_index_to_the_bundle(
    tmp_path: Path, index_closes: Counter[int], opened_indexes: list[SkillIndex]
) -> None:
    bundle = _build(tmp_path)
    try:
        assert opened_indexes == [bundle.index]
        assert index_closes == Counter()
        assert bundle.index._conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        bundle.close()
    assert index_closes == Counter({id(bundle.index): 1})
    _assert_closed(bundle.index)
