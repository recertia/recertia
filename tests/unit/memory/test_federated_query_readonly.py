"""Debug federated query must refuse a stale index, never rebuild it."""

from __future__ import annotations

from pathlib import Path

from recertia.memory.procedural.store import SkillStore
from recertia.memory.query import federated_query
from recertia.retrieval.index import SkillIndex


def test_federated_query_refuses_stale_index(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    facts = tmp_path / "facts"
    episodic = tmp_path / "episodic"
    work = tmp_path / "work"
    index_path = tmp_path / "index.db"
    skills.mkdir()
    facts.mkdir()
    episodic.mkdir()
    work.mkdir()
    SkillStore(skills)
    index = SkillIndex(index_path)
    index.close()

    payload = federated_query(
        "add editorconfig",
        skills_root=skills,
        facts_root=facts,
        episodic_root=episodic,
        index_path=index_path,
        workdir=work,
    )
    assert payload["error"] == "index_stale"
    assert payload["skills"]["returned"] == []


def test_federated_query_refuses_another_tenant_store(tmp_path: Path) -> None:
    root = tmp_path / "runs" / "acme"
    other = tmp_path / "runs" / "beta" / "episodic"
    for path in (root / "facts", root / "episodic", other):
        path.mkdir(parents=True)
    skills = tmp_path / "skills"
    skills.mkdir()
    try:
        federated_query(
            "hello",
            skills_root=skills,
            facts_root=root / "facts",
            episodic_root=other,
            index_path=root / "skill_index.db",
            workdir=root,
            tenant_id="acme",
            tenant_root=root,
        )
    except ValueError as exc:
        assert "episodic" in str(exc)
        assert "acme" in str(exc)
    else:
        raise AssertionError("expected another tenant's episodic store to be refused")


def test_federated_query_does_not_rebuild(tmp_path: Path, monkeypatch) -> None:
    skills = tmp_path / "skills"
    facts = tmp_path / "facts"
    episodic = tmp_path / "episodic"
    work = tmp_path / "work"
    index_path = tmp_path / "index.db"
    for path in (skills, facts, episodic, work):
        path.mkdir()
    SkillStore(skills)
    index = SkillIndex(index_path)
    index.close()

    def _boom(*_args, **_kwargs):
        raise AssertionError("debug query rebuilt the index")

    monkeypatch.setattr(SkillIndex, "rebuild", _boom)
    payload = federated_query(
        "add editorconfig",
        skills_root=skills,
        facts_root=facts,
        episodic_root=episodic,
        index_path=index_path,
        workdir=work,
    )
    assert payload["error"] == "index_stale"
