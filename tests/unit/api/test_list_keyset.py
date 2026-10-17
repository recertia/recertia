"""Keyset pages for facts, cases, and proposals."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from contracts.fact import Fact, FactProvenance
from recertia.api import create_app
from recertia.api.pagination import keyset_page
from recertia.memory.episodic import CaseRecord, EpisodicStore
from recertia.memory.semantic import FactStore
from recertia.proposals.store import ProposalRecord


def test_keyset_page_drops_nothing_and_stops() -> None:
    page, cursor = keyset_page(["c", "a", "b"], key=lambda item: item, limit=2, cursor=None)
    assert page == ["a", "b"]
    assert cursor == "b"
    rest, done = keyset_page(["c", "a", "b"], key=lambda item: item, limit=2, cursor=cursor)
    assert rest == ["c"]
    assert done is None


def test_http_lists_page_by_id(tmp_path: Path) -> None:
    app = create_app(root=tmp_path / "root")
    ctx = app.state.console_ctx
    runs = ctx.tenant_runs_root("t1")
    facts = FactStore(runs / "facts")
    for fact_id in ("c-fact", "a-fact", "b-fact"):
        facts.write(
            Fact(
                fact_id=fact_id,
                scope="project",
                slug=fact_id,
                assertion="stored for the list page",
                provenance=FactProvenance(asserting_run="test"),
                authored_at=datetime.now(timezone.utc),
            )
        )
    episodic = EpisodicStore(runs / "episodic")
    for case_id in ("c-case", "a-case", "b-case"):
        episodic.write(
            CaseRecord(case_id=case_id, run_id="run-1", attempt_no=1, outcome="solved")
        )
    for proposal_id in ("c-prop", "a-prop", "b-prop"):
        ctx.proposals.add(
            ProposalRecord(
                proposal_id=proposal_id,
                kind="mine",
                skill_id="skill",
                version=1,
                rationale="queued for review",
                tenant_id="t1",
            )
        )

    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    client = TestClient(app)
    headers = {"X-API-Key": issued.secret}

    def _pages(path: str, id_key: str) -> list[str]:
        first = client.get(path, params={"limit": 2}, headers=headers)
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["next_cursor"] == body["items"][-1][id_key]
        second = client.get(
            path, params={"limit": 2, "cursor": body["next_cursor"]}, headers=headers
        )
        assert second.status_code == 200, second.text
        assert second.json()["next_cursor"] is None
        return [row[id_key] for row in body["items"]] + [
            row[id_key] for row in second.json()["items"]
        ]

    assert _pages("/v1/facts", "fact_id") == ["a-fact", "b-fact", "c-fact"]
    assert _pages("/v1/cases", "case_id") == ["a-case", "b-case", "c-case"]
    assert _pages("/v1/proposals", "proposal_id") == ["a-prop", "b-prop", "c-prop"]
