"""M1 done-when criteria (docs/archive/2026-Q3/implementation-plan.md M1)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from recertia.memory.procedural.store import SkillStore
from recertia.retrieval.index import SkillIndex
from recertia.retrieval.pipeline import Retriever

REPO = Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPO / "skills"
PROBES_PATH = REPO / "evals" / "probes" / "repo-chore.json"


@pytest.fixture
def retriever(tmp_path: Path) -> Iterator[Retriever]:
    store = SkillStore(SKILLS_ROOT)
    index = SkillIndex(tmp_path / "index.db")
    try:
        # Dream-46 candidates are draft keeps for a soak experiment.
        # They must not dilute the M1 probe index used for precision@3.
        entries = [
            (v, s, st)
            for v, s, st in store.iter_loaded()
            if "dream46" not in (v.tags or [])
        ]
        index.rebuild(entries)
        yield Retriever(index)
    finally:
        index.close()


def _materialise_workdir(tmp_path: Path, files: dict[str, str]) -> Path:
    workdir = tmp_path / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        path = workdir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return workdir
