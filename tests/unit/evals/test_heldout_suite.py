"""REC-009 Amendment 2: the held-out runner takes the skill from task.json and masks output.

Synthetic split only, built under ``tmp_path`` with neutral dir names (h001, h002).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from recertia.app.evals import EvalService
from recertia.evals.golden import (
    HELDOUT_PATH_MASK,
    run_heldout_suite,
    run_task_class_gate,
)
from recertia.evals.heldout_manifest import HeldoutManifestError, compute_root_sha256
from recertia.evals.store import EvalStore
from recertia.memory.procedural.store import SkillStore
from tests.support.promotion_fixtures import leak_golden as _golden
from tests.support.promotion_fixtures import leak_skill as _skill

SKILL = "leak-probe"
SPLIT_ID = "heldout-synthetic-chore-v1"


def _split(tmp_path: Path, *, names: tuple[str, ...] = ("h001", "h002")) -> Path:
    # Each workspace holds a file: empty dirs cannot be hashed and fail closed.
    root = tmp_path / "decrypted" / SPLIT_ID
    _golden(root / "repo-chore" / names[0], command="test -f MUST_EXIST.txt", workspace_file="a.txt")
    if len(names) > 1:
        _golden(root / "repo-chore" / names[1], command="test -f NEVER.txt", workspace_file="b.txt")
    return root


def _manifest(tmp_path: Path, root: Path) -> Path:
    digest, count = compute_root_sha256(root)
    path = tmp_path / "public" / f"{SPLIT_ID}.manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "split_id": SPLIT_ID,
                "count": count,
                "hash_rule": "sha256-sorted-relpath-v1",
                "root_sha256": digest,
                "ciphertext_sha256": "c" * 64,
                "created_at": "2026-10-21T12:00:00-07:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _skills(tmp_path: Path) -> Path:
    store = SkillStore(tmp_path / "skills")
    store.write_candidate(
        _skill(
            1,
            command="python3 -c \"open('MUST_EXIST.txt','w').write('ok')\"",
            criterion_run="test -f MUST_EXIST.txt",
        )
    )
    return tmp_path / "skills"


def _run(tmp_path: Path, root: Path, manifest: Path, **kwargs: object) -> object:
    return run_heldout_suite(
        manifest_path=manifest,
        heldout_root=root,
        task_class="repo-chore",
        skills_root=_skills(tmp_path),
        runs_root=tmp_path / "runs",
        **kwargs,  # type: ignore[arg-type]
    )


def test_heldout_output_masks_golden_path_and_run_id(tmp_path: Path) -> None:
    root = _split(tmp_path)
    eval_db = tmp_path / "evals.db"
    store = EvalStore(eval_db)
    try:
        report = _run(tmp_path, root, _manifest(tmp_path, root), eval_store=store,
                      snapshot_id="heldout-test")
    finally:
        store.close()
    results = report.results  # type: ignore[attr-defined]
    assert [r.passed for r in results] == [True, False]
    assert all(r.skill_id == SKILL for r in results)  # from task.json, not the dir name
    for r in results:
        assert r.golden_path == HELDOUT_PATH_MASK
        assert r.run_id.startswith("golden-heldout-")
        assert SKILL not in r.run_id

    with sqlite3.connect(eval_db) as conn:  # the evals.db sink is not vacuous
        fixture_ids = [row[0] for row in conn.execute("SELECT fixture_id FROM observations")]
    assert len(fixture_ids) == 2
    assert all(f.startswith("golden-heldout-") for f in fixture_ids)

    out = tmp_path / "report.json"
    report.write(out)  # type: ignore[attr-defined]
    payload = json.dumps(EvalService().suite_payload(report))
    sinks = {"GoldenReport.write": out.read_text(encoding="utf-8"), "suite_payload": payload}
    for path in [eval_db, *(tmp_path / "runs").rglob("*")]:
        if path.is_file():
            sinks[path.name] = path.read_bytes().decode("utf-8", "replace")
    for name, text in sinks.items():
        for leaked in ("h001", "h002", str(root), str(tmp_path / "decrypted"), f"golden-{SKILL}"):
            assert leaked not in text, f"{leaked!r} in {name}"


def test_heldout_refuses_to_infer_the_skill_from_a_dir_name(tmp_path: Path) -> None:
    """A dir named after a real skill, with no expected_skill_id, refuses the whole run."""

    root = _split(tmp_path, names=("h001",))
    trap = root / "repo-chore" / SKILL
    trap.mkdir()
    (trap / "task.json").write_text('{"request": "synthetic"}\n', encoding="utf-8")
    (trap / "expect.json").write_text('{"terminal": "solved"}\n', encoding="utf-8")
    with pytest.raises(HeldoutManifestError, match="refusing to infer a skill") as exc:
        _run(tmp_path, root, _manifest(tmp_path, root))
    assert SKILL not in str(exc.value)
    assert not (tmp_path / "runs").exists()  # nothing ran

    goal_only = _split(tmp_path / "g", names=("h001",))
    (goal_only / "repo-chore" / "h001" / "task.json").unlink()
    (goal_only / "repo-chore" / "h001" / "goal.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(HeldoutManifestError, match="refusing to infer a skill"):
        _run(tmp_path / "g", goal_only, _manifest(tmp_path / "g", goal_only))


def test_heldout_suite_fails_closed_before_running_on_a_mismatch(tmp_path: Path) -> None:
    root = _split(tmp_path)
    manifest = _manifest(tmp_path, root)
    (root / "repo-chore" / "h002" / "expect.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(HeldoutManifestError, match="^held-out hash mismatch"):
        _run(tmp_path, root, manifest)
    assert not (tmp_path / "runs").exists()


def test_task_class_gate_runs_opaque_dirs_unchanged(tmp_path: Path) -> None:
    """CTO-rec 14:08: the task-class gate already handles h001… dirs; it is not modified."""

    root = _split(tmp_path)
    _skills(tmp_path)
    version = SkillStore(tmp_path / "skills").get_version(SKILL, 1)
    report = run_task_class_gate(version, root, runs_root=tmp_path / "runs")
    assert [Path(r.golden_path).name for r in report.results] == ["h001", "h002"]
    assert [r.passed for r in report.results] == [True, False]
