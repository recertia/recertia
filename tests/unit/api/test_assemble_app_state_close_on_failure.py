"""``assemble_app_state`` closes stores already opened when a later step raises.

Holders built at composition.py:59 (sessions), :60 (queue), :124 (proposals),
:125 (programs) and :126 (job_runs) must not leak when a later step fails.
Closed is shown by ``sqlite3.ProgrammingError`` on reuse — no close spy.
IdempotencyStore is on the stack but opens control.db lazily; reuse of an
unopened writer cannot be observed, so no ProgrammingError claim for it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

import recertia.api.composition as composition_mod
from recertia.api.composition import assemble_app_state


class _ForcedFailure(Exception):
    pass


def _noop(*_a: Any, **_k: Any) -> Any:
    return None


def _stub_kwargs(root: Path) -> dict[str, Any]:
    return {
        "root": root,
        "skills_root": root / "skills",
        "facts_root": root / "facts",
        "key_store": object(),
        "quota_store": object(),
        "runs": {},
        "run_slots": object(),
        "record_from_state": _noop,
        "load_from_checkpoints": _noop,
        "resolve_create_workdir": _noop,
        "persist_workdir": _noop,
        "canonical_run_workdir": _noop,
        "clamp_criteria": _noop,
        "principal_may_exec": _noop,
        "require_scope": _noop,
        "validate_run_id": _noop,
        "workspace_registry": object(),
        "run_service": object(),
        "model_config_for_run": _noop,
        "tenant_has_checkpoints": _noop,
        "max_criterion_timeout_s": _noop,
        "load_persisted_workdir": _noop,
        "settings": None,
    }


def _assert_closed(conn: sqlite3.Connection, holder: str) -> None:
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
        pytest.fail(f"{holder}: the connection is still open")


def _capture(name: str, cls: Any, holders: dict[str, Any], conns: dict[str, sqlite3.Connection]):
    def factory(*args: Any, **kwargs: Any) -> Any:
        obj = cls(*args, **kwargs)
        holders[name] = obj
        conn = getattr(obj, "_conn", None)
        if isinstance(conn, sqlite3.Connection):
            conns[name] = conn
        return obj

    return factory


def test_assemble_closes_sessions_and_queue_when_proposal_store_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail at ProposalStore (:124); sessions (:59) and queue (:60) must close."""

    root = tmp_path / "api-root"
    root.mkdir()
    holders: dict[str, Any] = {}
    conns: dict[str, sqlite3.Connection] = {}
    monkeypatch.setattr(
        composition_mod,
        "SessionStore",
        _capture("sessions", composition_mod.SessionStore, holders, conns),
    )
    real_open = composition_mod.open_run_queue

    def capture_queue(r: Any) -> Any:
        q = real_open(r)
        holders["queue"] = q
        conns["queue"] = q._conn
        return q

    monkeypatch.setattr(composition_mod, "open_run_queue", capture_queue)

    box: dict[str, BaseException] = {}

    def boom(*_a: Any, **_k: Any) -> Any:
        err = _ForcedFailure("ProposalStore forced failure")
        box["err"] = err
        raise err

    monkeypatch.setattr(composition_mod, "ProposalStore", boom)

    with pytest.raises(_ForcedFailure, match="ProposalStore forced failure") as caught:
        assemble_app_state(**_stub_kwargs(root))
    assert caught.value is box["err"]
    assert set(conns) >= {"sessions", "queue"}
    for name in ("sessions", "queue"):
        _assert_closed(conns[name], name)


def test_assemble_closes_all_sqlite_holders_when_app_state_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail after :126; sessions, queue, proposals, programs, job_runs must close."""

    root = tmp_path / "api-root"
    root.mkdir()
    holders: dict[str, Any] = {}
    conns: dict[str, sqlite3.Connection] = {}
    monkeypatch.setattr(
        composition_mod,
        "SessionStore",
        _capture("sessions", composition_mod.SessionStore, holders, conns),
    )
    real_open = composition_mod.open_run_queue

    def capture_queue(r: Any) -> Any:
        q = real_open(r)
        holders["queue"] = q
        conns["queue"] = q._conn
        return q

    monkeypatch.setattr(composition_mod, "open_run_queue", capture_queue)
    for name, cls in (
        ("proposals", composition_mod.ProposalStore),
        ("programs", composition_mod.ProgramStore),
        ("job_runs", composition_mod.JobRunStore),
    ):
        monkeypatch.setattr(composition_mod, cls.__name__, _capture(name, cls, holders, conns))

    box: dict[str, BaseException] = {}

    def boom_state(*_a: Any, **_k: Any) -> Any:
        err = _ForcedFailure("AppState forced failure")
        box["err"] = err
        raise err

    monkeypatch.setattr(composition_mod, "AppState", boom_state)

    with pytest.raises(_ForcedFailure, match="AppState forced failure") as caught:
        assemble_app_state(**_stub_kwargs(root))
    assert caught.value is box["err"]
    expected = {"sessions", "queue", "proposals", "programs", "job_runs"}
    assert expected <= set(conns), set(conns)
    for name in expected:
        _assert_closed(conns[name], name)
