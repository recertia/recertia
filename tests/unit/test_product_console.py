"""Product console C0–C5 conformance (PC-1…PC-6)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from recertia.api import create_app
from recertia.ledger import HashChainLedger
from recertia.memory.procedural.seeds import (
    add_gitignore_entry,
    seed_stats,
    seed_status_draft,
)
from recertia.memory.procedural.store import SkillStore
from recertia.proposals.store import ProposalRecord


def _goal() -> dict:
    return {
        "goal_id": "pc-goal-1",
        "desired": [
            {
                "id": "gitignore",
                "kind": "file_exists",
                "path": ".gitignore",
                "weight": 1.0,
            }
        ],
        "constraints": [],
        "context": "ensure gitignore exists",
        "task_class": "repo-chore",
    }


def _enable_dev_console(monkeypatch: pytest.MonkeyPatch, *, admin: bool = True) -> None:
    monkeypatch.setenv("RECERTIA_CONSOLE_AUTH", "dev")
    monkeypatch.setenv("RECERTIA_CONSOLE_DEV_LOGIN", "1")
    monkeypatch.setenv("RECERTIA_CONSOLE_SESSION_SECRET", "t" * 32)
    monkeypatch.setenv("RECERTIA_CONSOLE_COOKIE_SECURE", "0")
    if admin:
        monkeypatch.setenv("RECERTIA_CONSOLE_DEV_ADMIN", "1")
    else:
        monkeypatch.delenv("RECERTIA_CONSOLE_DEV_ADMIN", raising=False)


def _client(tmp_path: Path, *, skills_root: Path | None = None) -> tuple[TestClient, Any]:
    app = create_app(
        root=tmp_path / "api-root",
        skills_root=skills_root or (tmp_path / "skills"),
    )
    return TestClient(app), app


def _issue(app: Any, *, tenant_id: str = "t1", scopes: set[str] | None = None) -> dict[str, str]:
    issued = app.state.api_keys.issue(
        tenant_id=tenant_id,
        scopes=scopes or {"runs", "metrics", "exec", "admin"},
        actor="test",
    )
    return {"X-API-Key": issued.secret}


def test_pc1_list_runs_tenant_isolation(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    ha = _issue(app, tenant_id="tenant-a")
    hb = _issue(app, tenant_id="tenant-b")

    # Seed in-memory run records directly (avoid full graph for isolation check).
    from datetime import datetime, timezone

    from recertia.api import RunRecord

    app.state.runs[("tenant-a", "run-a1")] = RunRecord(
        run_id="run-a1",
        request="a",
        task_class="repo-chore",
        status="solved",
        created_at=datetime.now(timezone.utc),
        tenant_id="tenant-a",
        terminal="solved",
    )
    app.state.runs[("tenant-b", "run-b1")] = RunRecord(
        run_id="run-b1",
        request="b",
        task_class="repo-chore",
        status="solved",
        created_at=datetime.now(timezone.utc),
        tenant_id="tenant-b",
        terminal="solved",
    )

    listed_a = client.get("/v1/runs", headers=ha)
    assert listed_a.status_code == 200
    ids_a = {i["run_id"] for i in listed_a.json()["items"]}
    assert "run-a1" in ids_a
    assert "run-b1" not in ids_a
    assert all(i["tenant_id"] == "tenant-a" for i in listed_a.json()["items"])

    listed_b = client.get("/v1/runs", headers=hb)
    ids_b = {i["run_id"] for i in listed_b.json()["items"]}
    assert "run-b1" in ids_b
    assert "run-a1" not in ids_b

    # API-key-only caller cannot spoof another tenant via header.
    spoof = client.get("/v1/runs", headers={**ha, "X-Recertia-Tenant": "tenant-b"})
    assert spoof.status_code == 403


def test_pc2_promote_does_not_skip_golden_gate(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from contracts.criteria import SensitivityProof, SkillCertificationCriterion
    from contracts.skill import Hygiene, Provenance, SkillVersion, Step

    skills = tmp_path / "skills"
    client, app = _client(tmp_path, skills_root=skills)
    headers = _issue(app)

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Skill with no golden fixtures — gate must refuse approval.
    ver = SkillVersion(
        skill_id="no-golden-skill",
        version=1,
        title="No golden fixtures skill",
        intent="Used only to prove console promote cannot skip the golden gate.",
        task_class="repo-chore",
        steps=[
            Step(
                id="noop",
                tool="shell",
                intent="No-op step for promotion gate refusal test.",
                inputs={"command": "true"},
            )
        ],
        certification_criteria=[
            SkillCertificationCriterion(
                id="ok",
                kind="command",
                run="true",
                weight=1.0,
                preregistered=True,
                sensitivity_proof=SensitivityProof(
                    criterion_id="ok",
                    negative_fixture="false",
                    rejected=True,
                    checked_at=now,
                ),
            )
        ],
        provenance=Provenance(
            distilled_from_run="unit",
            distilled_at=now,
            curation="human_authored",
            derivation="hand_authored",
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=now),
    )
    store = SkillStore(skills)
    store.write_version(ver)
    store.write_status(seed_status_draft(ver))
    store.write_stats(seed_stats(ver))

    before = store.get_status(ver.skill_id, ver.version).lifecycle
    assert before == "draft"

    resp = client.post(
        f"/v1/skills/{ver.skill_id}/versions/{ver.version}/promote",
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert "failing_fixtures" in body or "error" in body

    after = store.get_status(ver.skill_id, ver.version).lifecycle
    assert after != "approved"
    assert after == "draft"


def test_pc3_proposal_decision_appends_human_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_dev_console(monkeypatch)
    client, app = _client(tmp_path)
    headers = _issue(app, tenant_id="t1")

    login = client.post(
        "/v1/auth/dev-login",
        json={
            "user_id": "alice-reviewer",
            "roles": ["operator", "reviewer"],
            "tenants": ["t1"],
        },
    )
    assert login.status_code == 200
    assert "session" not in login.json()
    assert client.cookies.get("recertia_session")
    headers = {**headers}  # cookie carries the console session

    prop = ProposalRecord(
        proposal_id="prop-pc3",
        kind="curator",
        skill_id="demo",
        version=1,
        rationale="bench low contribution",
        payload={"tier": "T1"},
        tenant_id="t1",
    )
    app.state.console_ctx.proposals.add(prop)

    decided = client.post(
        "/v1/proposals/prop-pc3/decision",
        headers=headers,
        json={"decision": "approve", "note": "looks good"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved"
    assert decided.json()["decision"]["actor"] == "alice-reviewer"

    ledger = HashChainLedger(tmp_path / "api-root" / "runs" / "t1" / "ledger.jsonl")
    entries = ledger.entries()
    assert entries
    assert entries[-1].actor == "alice-reviewer"
    assert entries[-1].evidence.get("kind") == "proposal_decision"


def test_pc4_sse_after_cursor_skips_seen_events(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app, tenant_id="t1")
    events = app.state.console_ctx.events
    e1 = events.append("run-sse", "run.queued", {"n": 1})
    e2 = events.append("run-sse", "run.started", {"n": 2})
    e3 = events.append("run-sse", "run.finished", {"n": 3, "terminal": "solved"})

    from datetime import datetime, timezone

    from recertia.api import RunRecord

    app.state.runs[("t1", "run-sse")] = RunRecord(
        run_id="run-sse",
        request="x",
        task_class="repo-chore",
        status="solved",
        created_at=datetime.now(timezone.utc),
        tenant_id="t1",
        terminal="solved",
    )

    first = client.get("/v1/runs/run-sse/events", headers=headers)
    assert first.status_code == 200
    text = first.text
    assert e1["event_id"] in text
    assert e3["event_id"] in text

    resumed = client.get(
        f"/v1/runs/run-sse/events?after={e2['event_id']}",
        headers=headers,
    )
    assert resumed.status_code == 200
    resumed_text = resumed.text
    # after= is exclusive: e2 itself must not reappear; e3 must.
    assert f"id: {e2['event_id']}" not in resumed_text
    assert e3["event_id"] in resumed_text
    # Terminal finished appears once on resume (event line), not events before cursor.
    assert resumed_text.count("event: run.finished") == 1
    assert f"id: {e1['event_id']}" not in resumed_text


def test_pc5_metrics_report_preserves_unavailable(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app, scopes={"runs", "metrics", "admin"})
    resp = client.get("/v1/metrics/report", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "unavailable" in body
    assert isinstance(body["unavailable"], dict)
    # Empty window must not invent silent zeros for practice_conversion / calibration.
    assert body["unavailable"].get("practice_conversion") or body.get("practice_conversion") is None
    assert "calibration_error" in body["unavailable"] or body.get("calibration_error") is None


def test_pc6_goal_preview_round_trips(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    resp = client.post("/v1/goals/preview", headers=headers, json={"goal": _goal()})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["goal"]["goal_id"] == "pc-goal-1"
    assert body["criteria"]
    assert any(c["id"] == "gitignore" for c in body["criteria"])

    bad = client.post(
        "/v1/goals/preview",
        headers=headers,
        json={"goal": {"desired": [{"id": "j", "kind": "judge", "weight": 1.0}], "constraints": []}},
    )
    assert bad.status_code == 422


def test_c2_async_run_returns_202_and_completes(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    created = client.post(
        "/v1/runs",
        headers=headers,
        json={
            "request": "async chore",
            "task_class": "repo-chore",
            "run_id": "async-1",
            "mode": "async",
            "budget": {"max_attempts": 1},
        },
    )
    assert created.status_code == 202, created.text
    assert created.json()["status"] == "queued"
    assert created.json()["mode"] == "async"

    deadline = time.time() + 30
    terminal = None
    while time.time() < deadline:
        got = client.get("/v1/runs/async-1", headers=headers)
        assert got.status_code == 200
        body = got.json()
        if body.get("terminal") or body.get("status") in {"solved", "unsolved", "abstained", "error"}:
            terminal = body.get("terminal") or body.get("status")
            break
        time.sleep(0.1)
    assert terminal is not None

    ev = client.get("/v1/runs/async-1/events", headers=headers)
    assert ev.status_code == 200
    assert "run.queued" in ev.text or "run.started" in ev.text


def test_c3_me_and_rbac_operator_cannot_decide_t2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_dev_console(monkeypatch)
    client, app = _client(tmp_path)
    headers = _issue(app, tenant_id="t1")

    op = client.post(
        "/v1/auth/dev-login",
        json={
            "user_id": "ops-only",
            "roles": ["operator"],
            "tenants": ["t1"],
        },
    )
    assert op.status_code == 200
    me = client.get("/v1/me")
    assert me.status_code == 200
    assert me.json()["user_id"] == "ops-only"
    assert "operator" in me.json()["roles"]

    prop = ProposalRecord(
        proposal_id="t2-prop",
        kind="correction",
        skill_id="demo",
        version=1,
        rationale="policy edit",
        payload={"tier": "T2"},
        tenant_id="t1",
    )
    app.state.console_ctx.proposals.add(prop)
    denied = client.post(
        "/v1/proposals/t2-prop/decision",
        headers=headers,
        json={"decision": "approve"},
    )
    assert denied.status_code == 403


def test_c4_templates_and_tower_summary(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    templates = client.get("/v1/templates", headers=headers)
    assert templates.status_code == 200
    ids = {t["id"] for t in templates.json()["templates"]}
    assert "add-gitignore-pyc" in ids

    detail = client.get("/v1/templates/add-gitignore-pyc", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["goal"]["desired"]

    summary = client.get("/v1/console/tower-summary", headers=headers)
    assert summary.status_code == 200
    body = summary.json()
    assert "active_cap_pressure" in body
    assert "pending_proposals" in body
    # honesty: practice conversion may be unavailable without practice arms
    assert body.get("practice_conversion") is None or isinstance(
        body.get("practice_conversion"), (int, float)
    )


def test_c5_tenant_switch_isolates_skills_and_proposals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_TENANT_SKILLS", "1")
    _enable_dev_console(monkeypatch)
    client, app = _client(tmp_path)
    # Key for tenant-a; session membership covers both.
    headers = _issue(app, tenant_id="tenant-a")
    login = client.post(
        "/v1/auth/dev-login",
        json={
            "user_id": "multi",
            "roles": ["admin", "reviewer", "operator"],
            "tenants": ["tenant-a", "tenant-b"],
            "active_tenant": "tenant-a",
        },
    )
    assert login.status_code == 200
    headers_a = {
        **headers,
        "X-Recertia-Tenant": "tenant-a",
    }

    ver = add_gitignore_entry()
    store_a = SkillStore(app.state.console_ctx.tenant_skills_root("tenant-a"))
    store_a.write_version(ver)
    store_a.write_status(seed_status_draft(ver))
    store_a.write_stats(seed_stats(ver))

    app.state.console_ctx.proposals.add(
        ProposalRecord(
            proposal_id="pa",
            kind="mine",
            skill_id=ver.skill_id,
            version=1,
            rationale="a only",
            tenant_id="tenant-a",
        )
    )
    app.state.console_ctx.proposals.add(
        ProposalRecord(
            proposal_id="pb",
            kind="mine",
            skill_id="other",
            version=1,
            rationale="b only",
            tenant_id="tenant-b",
        )
    )

    skills_a = client.get("/v1/skills", headers=headers_a)
    assert skills_a.status_code == 200
    assert any(s["skill_id"] == ver.skill_id for s in skills_a.json()["items"])

    props_a = client.get("/v1/proposals", headers=headers_a)
    ids_a = {p["proposal_id"] for p in props_a.json()["items"]}
    assert "pa" in ids_a
    assert "pb" not in ids_a

    switched = client.post(
        "/v1/auth/switch-tenant",
        json={"tenant_id": "tenant-b"},
    )
    assert switched.status_code == 200
    headers_b = {
        **headers,
        "X-Recertia-Tenant": "tenant-b",
    }
    props_b = client.get("/v1/proposals", headers=headers_b)
    ids_b = {p["proposal_id"] for p in props_b.json()["items"]}
    assert "pb" in ids_b
    assert "pa" not in ids_b

    skills_b = client.get("/v1/skills", headers=headers_b)
    assert skills_b.status_code == 200
    assert not any(s["skill_id"] == ver.skill_id for s in skills_b.json()["items"])


def test_console_static_served(tmp_path: Path) -> None:
    client, _app = _client(tmp_path)
    page = client.get("/console")
    assert page.status_code == 200
    assert "Recertia" in page.text
    css = client.get("/console/assets/styles.css")
    assert css.status_code == 200


def test_job_dry_run_does_not_persist_proposals(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    resp = client.post(
        "/v1/jobs/practice/run",
        headers=headers,
        json={"dry_run": True, "one_off": ["cluster-a"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["dry_run"] is True
    # dry_run may emit proposal payloads on the job record but must not enqueue durable queue
    pending = app.state.console_ctx.proposals.list(tenant_id="t1", status="pending")
    assert pending == []


def test_http_mine_defaults_to_hints_not_arxiv(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app)
    resp = client.post("/v1/jobs/mine/run", headers=headers, json={"dry_run": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "succeeded"
    proposals = body.get("proposals") or []
    assert proposals
    payload = proposals[0].get("payload") or {}
    assert payload.get("curation") == "mined_from_human_artifact"
    assert "arxiv_id" not in payload
    pending = app.state.console_ctx.proposals.list(tenant_id="t1", status="pending")
    assert pending == []


def test_http_mine_arxiv_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from recertia.jobs import Proposal

    def fake_mine(store, **kwargs):
        del store
        assert kwargs.get("arxiv_ids") == ["2605.22148"]
        return [
            Proposal(
                kind="mine",
                skill_id="arxiv-2605-22148",
                version=1,
                rationale="mined_from_paper",
                payload={"curation": "mined_from_paper", "arxiv_id": "2605.22148"},
            )
        ]

    monkeypatch.setattr("recertia.jobs.dispatch.mine_from_arxiv", fake_mine)
    client, _app = _client(tmp_path)
    headers = _issue(_app)
    resp = client.post(
        "/v1/jobs/mine/run",
        headers=headers,
        json={"dry_run": True, "arxiv_id": ["2605.22148"]},
    )
    assert resp.status_code == 200, resp.text
    proposals = resp.json().get("proposals") or []
    assert proposals[0]["payload"]["curation"] == "mined_from_paper"


def test_rejected_proposal_cannot_be_reapproved(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    headers = _issue(app, tenant_id="t1")
    app.state.console_ctx.proposals.add(
        ProposalRecord(
            proposal_id="rej1",
            kind="mine",
            skill_id="x",
            version=1,
            rationale="r",
            tenant_id="t1",
        )
    )
    r1 = client.post(
        "/v1/proposals/rej1/decision",
        headers=headers,
        json={"decision": "reject"},
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/v1/proposals/rej1/decision",
        headers=headers,
        json={"decision": "approve"},
    )
    assert r2.status_code == 400
