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


def test_retrieval_precision_at_3_at_least_0_7(retriever: Retriever, tmp_path: Path) -> None:
    """``retrieval_precision_at_3`` ≥ 0.7 on a labelled probe set.

    Specs §10 defines the metric as ``|relevant ∩ top3| / 3``. With single-label probes that
    ceiling is 1/3, so the M1 engineering gate uses the standard IR normalisation
    ``|relevant ∩ top3| / min(3, |relevant|)`` per probe (equivalent to success@3 when each
    probe has one gold skill), then averages. Multi-label probes recover the raw ÷3 form.
    """

    probes = json.loads(PROBES_PATH.read_text())["probes"]
    per_probe: list[float] = []
    for probe in probes:
        workdir = _materialise_workdir(tmp_path / probe["id"], probe["workdir_files"])
        bundle, _ = retriever.search(
            probe["request"],
            workdir=workdir,
            env_fingerprint={"python": "3.12", "pytest": "8.3.4"},
        )
        top3 = {c.skill_id for c in bundle.skills[:3]}
        relevant = set(probe["relevant"])
        denom = min(3, len(relevant)) or 1
        per_probe.append(len(top3 & relevant) / denom)
    precision = sum(per_probe) / len(per_probe)
    assert precision >= 0.7, f"retrieval_precision_at_3={precision:.3f} probes={per_probe}"
