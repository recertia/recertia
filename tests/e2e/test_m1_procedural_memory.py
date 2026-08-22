"""M1 done-when criteria (docs/archive/2026-Q3/implementation-plan.md M1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from recertia.memory.procedural.store import SkillStore
from recertia.retrieval.index import SkillIndex
from recertia.retrieval.pipeline import Retriever

REPO = Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPO / "skills"
PROBES_PATH = REPO / "evals" / "probes" / "repo-chore.json"


@pytest.fixture
def retriever(tmp_path: Path) -> Retriever:
    store = SkillStore(SKILLS_ROOT)
    index = SkillIndex(tmp_path / "index.db")
    index.rebuild(store.iter_loaded())
    return Retriever(index)


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


def test_unrelated_task_returns_empty_bundle(retriever: Retriever, tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    bundle, explanation = retriever.search(
        "translate this Portuguese poem into Klingon hexameter",
        workdir=workdir,
    )
    assert bundle.skills == []
    assert explanation.returned == []


def test_env_fingerprint_mismatch_is_dropped_not_demoted(
    retriever: Retriever, tmp_path: Path
) -> None:
    workdir = _materialise_workdir(tmp_path, {".gitignore": ".venv/\n"})
    # Seed skills declare python=3.12; a conflicting run fingerprint must hard-drop.
    bundle, explanation = retriever.search(
        "Add *.pyc to the repository .gitignore",
        workdir=workdir,
        env_fingerprint={"python": "3.11"},
    )
    dropped = [d for d in explanation.dropped if d.stage == "env_fingerprint"]
    assert dropped, "expected at least one env_fingerprint drop"
    assert all(d.skill_id != "add-gitignore-entry" or d.stage == "env_fingerprint"
               for d in explanation.dropped if d.skill_id == "add-gitignore-entry")
    # The matching skill must not appear in the returned bundle.
    assert all(c.skill_id != "add-gitignore-entry" for c in bundle.skills)
    # And it must not appear merely as a demotion — demotion is for thin evidence, not env mismatch.
    assert all(sid != "add-gitignore-entry" for sid, *_ in explanation.demoted)


def test_thin_evidence_is_demoted_never_hard_dropped(
    retriever: Retriever, tmp_path: Path
) -> None:
    workdir = _materialise_workdir(tmp_path, {".gitignore": ".venv/\n"})
    bundle, explanation = retriever.search(
        "Add *.pyc to the repository .gitignore",
        workdir=workdir,
        env_fingerprint={"python": "3.12", "pytest": "8.3.4"},
    )
    # Seed skills have applications=0 < evidence_floor → demotion reason recorded.
    demoted_ids = {sid for sid, *_ in explanation.demoted}
    assert "add-gitignore-entry" in demoted_ids
    # But still returned (demotion ≠ drop).
    assert any(c.skill_id == "add-gitignore-entry" for c in bundle.skills)
    assert not any(
        d.skill_id == "add-gitignore-entry" and "thin_evidence" in d.reason
        for d in explanation.dropped
    )


def test_novel_task_routes_to_scratch(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from contracts.criteria import SensitivityProof, TaskCriterion
    from contracts.run import Task
    from recertia.graph.engine import GraphOrchestrator
    from recertia.memory.procedural.store import SkillStore
    from recertia.retrieval.index import SkillIndex
    from recertia.retrieval.pipeline import Retriever

    store = SkillStore(SKILLS_ROOT)
    index = SkillIndex(tmp_path / "index.db")
    index.rebuild(store.iter_loaded())
    retriever = Retriever(index)

    workdir = tmp_path / "work"
    workdir.mkdir()
    orch = GraphOrchestrator(
        tmp_path / "runs",
        retriever=retriever,
        store=store,
        env_fingerprint={"python": "3.12"},
    )
    try:
        state = orch.start(
            "novel-1",
            Task(
                task_id="novel-1",
                request="compose a twelve-tone serialist symphony about garbage collection",
                submitted_at=datetime.now(timezone.utc),
            ),
            [
                TaskCriterion(
                    id="ok",
                    kind="command",
                    run="true",
                    source="caller",
                    weight=1.0,
                    sensitivity_proof=SensitivityProof(
                        criterion_id="ok",
                        negative_fixture="false",
                        rejected=True,
                        checked_at=datetime.now(timezone.utc),
                    ),
                )
            ],
            workdir=workdir,
            script=["true"],
        )
    finally:
        orch.close()
        index.close()

    assert state.strategy == "scratch"
    assert state.terminal == "solved"


def test_every_seed_skill_has_golden_promotion_log() -> None:
    """Every seed skill passed its golden task before approved; the log is the evidence."""

    from recertia.memory.procedural.seeds import SEED_SKILLS

    store = SkillStore(SKILLS_ROOT)
    log_dir = REPO / "evals" / "golden" / "_promotion_logs"
    for version in SEED_SKILLS:
        status = store.get_status(version.skill_id, version.version)
        assert status.lifecycle == "approved"
        assert status.active is True
        assert status.certification.golden_set_ref
        log_path = _resolve_repo_path(status.certification.golden_set_ref)
        assert log_path.exists(), (
            f"missing golden log for {version.skill_id} "
            f"(ref={status.certification.golden_set_ref!r} → {log_path})"
        )
        payload = json.loads(log_path.read_text())
        passed = payload.get("passed")
        if passed is None:
            passed = payload.get("all_passed")
        assert passed is True
        assert log_path.parent == log_dir


def _resolve_repo_path(ref: str) -> Path:
    """Resolve a golden_set_ref that may be repo-relative or a legacy absolute path."""

    path = Path(ref)
    if path.is_absolute():
        # Legacy absolute refs from early seed installs — map onto this checkout.
        if "evals" in path.parts:
            idx = path.parts.index("evals")
            return REPO.joinpath(*path.parts[idx:])
        return path
    return (REPO / path).resolve()


def test_matching_task_routes_to_apply(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from contracts.criteria import SensitivityProof, TaskCriterion
    from contracts.run import Task
    from recertia.graph.engine import GraphOrchestrator
    from recertia.memory.procedural.store import SkillStore
    from recertia.retrieval.index import SkillIndex
    from recertia.retrieval.pipeline import Retriever

    store = SkillStore(SKILLS_ROOT)
    index = SkillIndex(tmp_path / "index.db")
    index.rebuild(store.iter_loaded())
    retriever = Retriever(index)

    workdir = _materialise_workdir(tmp_path, {".gitignore": ".venv/\n"})
    orch = GraphOrchestrator(
        tmp_path / "runs",
        retriever=retriever,
        store=store,
        env_fingerprint={"python": "3.12", "pytest": "8.3.4"},
    )
    try:
        state = orch.start(
            "apply-1",
            Task(
                task_id="apply-1",
                request="Add *.pyc to the repository .gitignore",
                task_class="repo-chore",
                submitted_at=datetime.now(timezone.utc),
            ),
            [
                TaskCriterion(
                    id="has-entry",
                    kind="command",
                    run="grep -qxF '*.pyc' .gitignore",
                    source="caller",
                    weight=1.0,
                    sensitivity_proof=SensitivityProof(
                        criterion_id="has-entry",
                        negative_fixture="gitignore without *.pyc",
                        rejected=True,
                        checked_at=datetime.now(timezone.utc),
                    ),
                )
            ],
            workdir=workdir,
            # No explicit script — solve derives it from the chosen skill.
            script=None,
        )
    finally:
        orch.close()
        index.close()

    assert state.strategy == "apply"
    assert state.chosen is not None
    assert state.chosen.skill_id == "add-gitignore-entry"
    assert state.terminal == "solved"
    assert (workdir / ".gitignore").read_text().find("*.pyc") >= 0
