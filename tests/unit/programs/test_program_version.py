"""REL-5 F20: program If-Match and durable idempotency keys."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from contracts.program import MigrationProgram, MigrationStep
from recertia.app.state import AppState
from recertia.persistence.idempotency import IdempotencyStore
from recertia.programs.store import ProgramStore
from recertia.runtime.errors import IdempotencyMismatch, ProgramConflict

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from recertia.api import create_app


def _step() -> dict[str, Any]:
    return {
        "step_id": "s1",
        "ordinal": 0,
        "title": "s1",
        "role": "custom",
        "goal": {
            "desired": [{"id": "d", "kind": "file_exists", "path": "README.md", "weight": 1.0}],
            "context": "c",
            "task_class": "repo-chore",
        },
    }


def _program(*, tenant_id: str = "t1", title: str = "pack") -> MigrationProgram:
    return MigrationProgram(
        program_id="p1",
        tenant_id=tenant_id,
        title=title,
        steps=[MigrationStep.model_validate(_step())],
    )


def _client(tmp_path: Path) -> tuple[TestClient, Any]:
    app = create_app(root=tmp_path / "api-root", skills_root=tmp_path / "skills")
    return TestClient(app), app


def _issue(app: Any, *, tenant_id: str = "t1") -> dict[str, str]:
    issued = app.state.api_keys.issue(
        tenant_id=tenant_id,
        scopes={"runs", "metrics", "exec", "admin"},
        actor="test",
    )
    return {"X-API-Key": issued.secret}


def test_stale_if_match_is_program_conflict(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    created = client.post(
        "/v1/programs",
        headers=headers,
        json={"title": "pack", "intent": "move", "steps": [_step()]},
    )
    assert created.status_code == 200, created.text
    body = created.json()["program"]
    pid = body["program_id"]
    first = client.put(
        f"/v1/programs/{pid}",
        headers={**headers, "If-Match": "1"},
        json=body,
    )
    assert first.status_code == 200, first.text
    assert first.json()["program"]["version"] == 2
    stale = client.put(
        f"/v1/programs/{pid}",
        headers={**headers, "If-Match": "1"},
        json=body,
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "program_conflict"
    missing = client.put(f"/v1/programs/{pid}", headers=headers, json=body)
    assert missing.status_code == 428
    assert missing.json()["error"]["code"] == "precondition_required"


def test_idempotency_survives_restart(tmp_path: Path) -> None:
    control = tmp_path / "control.db"
    programs = tmp_path / "programs.sqlite"
    idem = IdempotencyStore(control)
    store = ProgramStore(programs, idempotency=idem)
    saved = store.put(_program(), idempotency_key="k1", idempotency_hash="abc")
    assert saved.version == 1
    store.close()
    idem.close()

    idem2 = IdempotencyStore(control)
    store2 = ProgramStore(programs, idempotency=idem2)
    replay = store2.put(
        _program(title="changed"),
        if_match=1,
        idempotency_key="k1",
        idempotency_hash="abc",
    )
    assert replay.title == "pack"
    assert replay.version == 1
    with pytest.raises(IdempotencyMismatch):
        store2.put(
            _program(title="other"),
            if_match=1,
            idempotency_key="k1",
            idempotency_hash="other",
        )
    store2.close()
    idem2.close()

    assert "_program_idempotency" not in AppState.__dataclass_fields__
    assert not (Path(__file__).resolve().parents[3] / "src/recertia/api/console_deps.py").exists()


def test_store_stale_if_match_raises(tmp_path: Path) -> None:
    store = ProgramStore(tmp_path / "programs.sqlite")
    try:
        saved = store.put(_program())
        assert saved.version == 1
        store.put(_program(title="v2"), if_match=1)
        with pytest.raises(ProgramConflict):
            store.put(_program(title="stale"), if_match=1)
    finally:
        store.close()
