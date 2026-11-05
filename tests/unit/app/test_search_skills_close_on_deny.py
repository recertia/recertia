"""Policy denial must still close the per-request SkillIndex (REC-134 / P18f).

``LibraryService.search_skills`` closes ``index`` in its success-path ``finally``
blocks, but ``_require`` ran before either ``try``. A PDP denial therefore leaked
the sqlite connection the route opened at ``api/routes/skills.py:125``.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from recertia.app.library import LibraryService
from recertia.errors import Forbidden
from recertia.ports.identity import Principal
from recertia.retrieval.index import SkillIndex

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from recertia.api import create_app


class _DenySkillsSearch:
    def require(self, principal: Principal, action: str, resource: Any) -> None:
        del principal, resource
        if action == "skills.search":
            raise Forbidden("skills.search denied")


class _CloseSpy:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def test_search_skills_closes_index_when_policy_denies() -> None:
    """Fail-first: at base, close is never reached because ``_require`` raises first."""

    service = LibraryService(pdp=_DenySkillsSearch())
    index = _CloseSpy()
    principal = Principal.from_api_key(
        key_id="k", tenant_id="t1", scopes=frozenset({"runs"})
    )
    with pytest.raises(Forbidden, match="skills.search denied") as caught:
        service.search_skills(
            principal, store=object(), index=index, query="q", limit=1
        )
    assert caught.value.args == ("skills.search denied",)
    assert index.closed == 1


def test_skills_search_route_closes_index_when_policy_denies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive ``skills.py:125`` so a strict ResourceWarning run can attribute the site."""

    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "local")
    monkeypatch.setenv("RECERTIA_API_ALLOW_LOCAL_EXEC", "1")
    app = create_app(root=tmp_path / "api-root", skills_root=tmp_path / "skills")
    app.state.recertia.library_service._pdp = _DenySkillsSearch()
    closes: Counter[int] = Counter()
    opened: list[SkillIndex] = []
    real_init = SkillIndex.__init__
    real_close = SkillIndex.close

    def spy_init(self: SkillIndex, *args: Any, **kwargs: Any) -> None:
        real_init(self, *args, **kwargs)
        opened.append(self)

    def spy_close(self: SkillIndex) -> None:
        closes[id(self)] += 1
        real_close(self)

    monkeypatch.setattr(SkillIndex, "__init__", spy_init)
    monkeypatch.setattr(SkillIndex, "close", spy_close)
    runs_only = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    client = TestClient(app)
    resp = client.post(
        "/v1/skills/search",
        json={"query": "q", "limit": 1},
        headers={"X-API-Key": runs_only.secret},
    )
    assert resp.status_code == 403
    assert len(opened) == 1
    assert closes == Counter({id(opened[0]): 1})
