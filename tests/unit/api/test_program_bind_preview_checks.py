"""Binding a run to a program step checks the hash the last preview stored.

``run_step`` used to replace the step's ``criteria_preview_hash`` with the hash of the goal
it had just materialized before the two bind checks read it, so "preview step before bind"
and "goal changed since preview" could never fire. They now read the stored hash, and a
rejected bind stores nothing, so it does not count as a preview either.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from contracts.program import MigrationProgram
from recertia.api import RunRecord
from recertia.programs.materialize import materialize_step_goal, preview_hash
from tests.support.http import error_text
from tests.unit.test_migration_programs import _client, _issue, _step


def _program(client: TestClient, headers: dict[str, str], **extra: Any) -> str:
    created = client.post(
        "/v1/programs", headers=headers, json={"title": "p", "steps": [_step("s1", 0, **extra)]}
    )
    assert created.status_code == 200, created.text
    pid = str(created.json()["program"]["program_id"])
    accepted = client.post(f"/v1/programs/{pid}/accept", headers=headers, json={"ack_disclaimer": True})
    assert accepted.status_code == 200, accepted.text
    return pid


def _stored_step(client: TestClient, headers: dict[str, str], pid: str) -> dict[str, Any]:
    got = client.get(f"/v1/programs/{pid}", headers=headers)
    assert got.status_code == 200, got.text
    steps = got.json()["program"]["steps"]
    return dict(next(s for s in steps if s["step_id"] == "s1"))


def _solved_run_matching_the_current_goal(
    app: Any, client: TestClient, headers: dict[str, str], pid: str, run_id: str
) -> None:
    """A solved run whose criteria hash is the one a bind materializes now, so only the
    preview checks can reject the bind."""

    got = client.get(f"/v1/programs/{pid}", headers=headers)
    prog = MigrationProgram.model_validate(got.json()["program"])
    step = next(s for s in prog.steps if s.step_id == "s1")
    app.state.runs[("t1", run_id)] = RunRecord(
        run_id=run_id,
        request="x",
        task_class="repo-chore",
        status="solved",
        created_at=datetime.now(timezone.utc),
        tenant_id="t1",
        terminal="solved",
        has_goal=True,
        criteria_hash=preview_hash(materialize_step_goal(prog, step)),
    )


def _bind(client: TestClient, headers: dict[str, str], pid: str, run_id: str) -> Any:
    return client.post(
        f"/v1/programs/{pid}/steps/s1/run", headers=headers, json={"bind_run_id": run_id, "workdir": "ws"}
    )


def test_bind_without_a_preview_is_rejected_and_stores_nothing(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    pid = _program(client, headers)
    _solved_run_matching_the_current_goal(app, client, headers, pid, "run-1")
    before = _stored_step(client, headers, pid)
    assert before["criteria_preview_hash"] is None

    for _ in range(2):  # a rejected bind is not a preview: the retry is rejected too
        resp = _bind(client, headers, pid, "run-1")
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "bad_request"
        assert error_text(resp) == "preview step before bind (missing criteria_preview_hash)"
        assert _stored_step(client, headers, pid) == before

    previewed = client.post(f"/v1/programs/{pid}/steps/s1/preview", headers=headers)
    assert previewed.status_code == 200, previewed.text
    bound = _bind(client, headers, pid, "run-1")
    assert bound.status_code == 200, bound.text
    assert bound.json()["step_status"] == "succeeded"


def test_bind_after_the_goal_was_patched_needs_a_new_preview(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    pid = _program(client, headers)
    assert client.post(f"/v1/programs/{pid}/steps/s1/preview", headers=headers).status_code == 200
    new_goal = _step("s1", 0, path="CHANGELOG.md")["goal"]
    patched = client.patch(f"/v1/programs/{pid}/steps/s1", headers=headers, json={"goal": new_goal})
    assert patched.status_code == 200, patched.text
    _solved_run_matching_the_current_goal(app, client, headers, pid, "run-2")

    resp = _bind(client, headers, pid, "run-2")
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "bad_request"
    assert error_text(resp) == "preview step before bind (missing criteria_preview_hash)"
    assert _stored_step(client, headers, pid)["criteria_preview_hash"] is None


def test_bind_after_the_materialized_goal_changed_since_the_preview_is_rejected(tmp_path: Path) -> None:
    """With hard freeze enforcement the freeze paths become goal constraints, and patching
    them keeps the stored preview hash: the bind must notice the goal moved."""

    client, app = _client(tmp_path)
    headers = _issue(app)
    created = client.post(
        "/v1/programs",
        headers=headers,
        json={"title": "p", "freeze_enforcement": "hard", "steps": [_step("s1", 0, freeze=["docs"])]},
    )
    assert created.status_code == 200, created.text
    pid = created.json()["program"]["program_id"]
    assert (
        client.post(f"/v1/programs/{pid}/accept", headers=headers, json={"ack_disclaimer": True}).status_code
        == 200
    )
    previewed = client.post(f"/v1/programs/{pid}/steps/s1/preview", headers=headers)
    assert previewed.status_code == 200, previewed.text
    old_hash = previewed.json()["criteria_preview_hash"]
    patched = client.patch(
        f"/v1/programs/{pid}/steps/s1", headers=headers, json={"freeze_paths": ["docs", "schema"]}
    )
    assert patched.status_code == 200, patched.text
    assert _stored_step(client, headers, pid)["criteria_preview_hash"] == old_hash
    _solved_run_matching_the_current_goal(app, client, headers, pid, "run-3")

    for _ in range(2):
        resp = _bind(client, headers, pid, "run-3")
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "conflict"
        assert error_text(resp) == "goal changed since preview; re-run preview before bind"
        assert _stored_step(client, headers, pid)["criteria_preview_hash"] == old_hash

    assert client.post(f"/v1/programs/{pid}/steps/s1/preview", headers=headers).status_code == 200
    bound = _bind(client, headers, pid, "run-3")
    assert bound.status_code == 200, bound.text
    assert bound.json()["step_status"] == "succeeded"


def test_plan_only_still_stores_the_preview_hash(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    pid = _program(client, headers)
    plan = client.post(f"/v1/programs/{pid}/steps/s1/run", headers=headers, json={"plan_only": True})
    assert plan.status_code == 200, plan.text
    assert _stored_step(client, headers, pid)["criteria_preview_hash"] == plan.json()["criteria_preview_hash"]
    _solved_run_matching_the_current_goal(app, client, headers, pid, "run-4")
    assert _bind(client, headers, pid, "run-4").status_code == 200


class _PutCounter:
    """Counts writes to the program store, so a test can tell "stored nothing" apart from
    "stored the same program again" (a write still moves ``updated_at``)."""

    def __init__(self, app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        store = app.state.recertia.programs
        self.calls = 0
        real_put = store.put

        def put(program: MigrationProgram) -> MigrationProgram:
            self.calls += 1
            return real_put(program)

        monkeypatch.setattr(store, "put", put)


def _stored(app: Any, pid: str) -> dict[str, Any]:
    prog = app.state.recertia.programs.get(pid, tenant_id="t1")
    assert prog is not None
    return prog.model_dump(mode="json")


def _bind_storing_nothing(
    app: Any,
    client: TestClient,
    headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    pid: str,
    run_id: str,
) -> Any:
    before = _stored(app, pid)
    puts = _PutCounter(app, monkeypatch)
    resp = _bind(client, headers, pid, run_id)
    assert puts.calls == 0, resp.text
    assert _stored(app, pid) == before
    monkeypatch.undo()
    return resp


def _previewed_program(client: TestClient, headers: dict[str, str]) -> str:
    pid = _program(client, headers)
    assert client.post(f"/v1/programs/{pid}/steps/s1/preview", headers=headers).status_code == 200
    return pid


def test_bind_to_an_unknown_run_is_rejected_and_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    pid = _previewed_program(client, headers)
    resp = _bind_storing_nothing(app, client, headers, monkeypatch, pid, "no-such-run")
    assert resp.status_code == 404, resp.text
    assert error_text(resp) == "run not found for tenant"


def test_bind_to_an_unknown_run_answers_404_before_the_preview_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run lookup comes before the preview checks: with no preview and no such run, the
    answer is ``404``, not the ``400`` a known run would get."""

    client, app = _client(tmp_path)
    headers = _issue(app)
    pid = _program(client, headers)
    resp = _bind_storing_nothing(app, client, headers, monkeypatch, pid, "no-such-run")
    assert resp.status_code == 404, resp.text
    assert error_text(resp) == "run not found for tenant"


def test_bind_to_a_program_that_is_not_active_is_rejected_and_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    pid = _previewed_program(client, headers)
    _solved_run_matching_the_current_goal(app, client, headers, pid, "run-5")
    abandoned = client.post(f"/v1/programs/{pid}/abandon", headers=headers)
    assert abandoned.status_code == 200, abandoned.text
    resp = _bind_storing_nothing(app, client, headers, monkeypatch, pid, "run-5")
    assert resp.status_code == 409, resp.text
    assert error_text(resp) == "program is not active"


def test_bind_before_the_previous_step_succeeded_is_rejected_and_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    created = client.post(
        "/v1/programs", headers=headers, json={"title": "p", "steps": [_step("s0", 0), _step("s1", 1)]}
    )
    assert created.status_code == 200, created.text
    pid = created.json()["program"]["program_id"]
    accepted = client.post(f"/v1/programs/{pid}/accept", headers=headers, json={"ack_disclaimer": True})
    assert accepted.status_code == 200, accepted.text
    assert client.post(f"/v1/programs/{pid}/steps/s1/preview", headers=headers).status_code == 200
    _solved_run_matching_the_current_goal(app, client, headers, pid, "run-6")
    resp = _bind_storing_nothing(app, client, headers, monkeypatch, pid, "run-6")
    assert resp.status_code == 409, resp.text
    assert error_text(resp) == "previous step s0 not succeeded"


def test_bind_to_a_run_with_another_criteria_hash_is_rejected_and_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    pid = _previewed_program(client, headers)
    _solved_run_matching_the_current_goal(app, client, headers, pid, "run-7")
    rec = app.state.runs[("t1", "run-7")]
    app.state.runs[("t1", "run-7")] = rec.model_copy(update={"criteria_hash": "0" * 64})
    resp = _bind_storing_nothing(app, client, headers, monkeypatch, pid, "run-7")
    assert resp.status_code == 409, resp.text
    assert error_text(resp).startswith("bound run criteria_hash does not match step criteria_preview_hash")


def test_an_idempotent_rebind_stores_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rebinding the step's current run answers ``idempotent`` without a write.

    The branch needs an active program whose step is still runnable (``ready``) while its
    ``current_run_id`` is already the run, so that state is seeded in the store; the run's
    criteria hash matches the previewed goal, so every earlier check passes."""

    client, app = _client(tmp_path)
    headers = _issue(app)
    pid = _previewed_program(client, headers)
    _solved_run_matching_the_current_goal(app, client, headers, pid, "run-8")
    store = app.state.recertia.programs
    prog = store.get(pid, tenant_id="t1")
    assert prog is not None and prog.status == "active"
    steps = [
        s.model_copy(update={"status": "ready", "current_run_id": "run-8", "run_ids": ["run-8"]})
        if s.step_id == "s1"
        else s
        for s in prog.steps
    ]
    store.put(prog.model_copy(update={"steps": steps}))
    resp = _bind_storing_nothing(app, client, headers, monkeypatch, pid, "run-8")
    assert resp.status_code == 200, resp.text
    assert resp.json()["idempotent"] is True
