"""Eval-only retrieval intercept stays off the production path."""

from __future__ import annotations

from pathlib import Path

from contracts.run import MemoryBundle, SkillCandidateRef
from recertia.memory.procedural.store import SkillStore
from recertia.retrieval.index import SkillIndex
from recertia.retrieval.pipeline import Retriever


def test_production_retriever_has_no_bundle_hook(tmp_path: Path) -> None:
    index = SkillIndex(tmp_path / "index.db")
    retriever = Retriever(index)
    assert retriever.bundle_hook is None
    bundle, _ = retriever.search("anything", workdir=tmp_path)
    assert bundle.skills == []
    index.close()


def test_bundle_hook_can_swap_a_candidate(tmp_path: Path) -> None:
    index = SkillIndex(tmp_path / "index.db")

    def hook(bundle: MemoryBundle) -> MemoryBundle:
        return MemoryBundle(
            skills=[SkillCandidateRef(skill_id="intervened", version=1, score=1.0)]
        )

    retriever = Retriever(index, bundle_hook=hook)
    bundle, _ = retriever.search("anything", workdir=tmp_path)
    assert [c.skill_id for c in bundle.skills] == ["intervened"]
    index.close()


def test_bootstrap_style_construction_does_not_pass_a_hook(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    index = SkillIndex(tmp_path / "index.db")
    index.rebuild(store.iter_loaded())
    retriever = Retriever(index)
    assert retriever.bundle_hook is None
    index.close()


def test_bootstrap_source_does_not_pass_bundle_hook() -> None:
    root = Path(__file__).resolve().parents[3]
    for rel in (
        "src/recertia/bootstrap.py",
        "src/recertia/cli/skills.py",
        "src/recertia/memory/query.py",
    ):
        text = (root / rel).read_text(encoding="utf-8")
        assert "bundle_hook" not in text, rel


def test_bundle_hook_cannot_be_assigned_after_construction(tmp_path: Path) -> None:
    index = SkillIndex(tmp_path / "index.db")
    retriever = Retriever(index)
    try:
        retriever.bundle_hook = lambda bundle: bundle  # type: ignore[misc]
        raised = False
    except AttributeError:
        raised = True
    index.close()
    assert raised
