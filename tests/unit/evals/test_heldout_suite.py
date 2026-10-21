"""REC-009 Amendment 2: the held-out runner takes the skill from task.json and masks output.

Synthetic split only, built under ``tmp_path`` with neutral dir names (h001, h002).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

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


# ----- M3: held-out run state stays out of checkouts and is removed after each run -----

MARK_REQUEST = "MARKREQUEST7f3a"
MARK_COMMAND = "MARKCMD91bd"
MARK_WS_FILE = "MARKWSFILE55e1"
MARKS = (MARK_REQUEST, MARK_COMMAND, MARK_WS_FILE)


def _marked_split(tmp_path: Path) -> Path:
    """h001 runs (request, criteria command, workspace file all marked); h002 is refused."""

    root = tmp_path / "decrypted" / SPLIT_ID
    _golden(
        root / "repo-chore" / "h001",
        command=f"test -f MUST_EXIST.txt || echo {MARK_COMMAND}",
        workspace_file=f"{MARK_WS_FILE}.txt",
        request=f"please do {MARK_REQUEST}",
    )
    h2 = _golden(
        root / "repo-chore" / "h002",
        command=f"true {MARK_COMMAND}",
        workspace_file=f"{MARK_WS_FILE}-b.txt",
        request=f"also {MARK_REQUEST}",
    )
    spec = json.loads((h2 / "task.json").read_text(encoding="utf-8"))
    spec["criteria"] = [{"id": "x", "kind": "command"}]  # refused: no command, no proof
    (h2 / "task.json").write_text(json.dumps(spec) + "\n", encoding="utf-8")
    return root


def _marker_hits(base: Path, *, skip: tuple[Path, ...]) -> list[str]:
    hits: list[str] = []
    for path in base.rglob("*"):
        if any(path == s or s in path.parents for s in skip):
            continue
        rel = path.relative_to(base).as_posix()
        for mark in MARKS:
            if mark in rel:
                hits.append(f"NAME {rel}")
        if path.is_file():
            data = path.read_bytes()
            hits.extend(f"CONTENT {rel} {mark}" for mark in MARKS if mark.encode() in data)
    return hits


def test_heldout_content_never_persists_in_run_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Request text, criteria commands and workspace names stay out of runtime.db & co."""

    from recertia.evals import golden

    started: list[Path] = []
    original = golden._start_orchestrator

    def _spy(runs_root: Path, **kwargs: object) -> object:
        started.append(runs_root)
        return original(runs_root, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(golden, "_start_orchestrator", _spy)
    root = _marked_split(tmp_path)
    eval_db = tmp_path / "evals.db"
    store = EvalStore(eval_db)
    try:
        report = _run(tmp_path, root, _manifest(tmp_path, root), eval_store=store,
                      snapshot_id="heldout-test")
    finally:
        store.close()
    results = report.results  # type: ignore[attr-defined]
    assert [r.detail for r in results][1] == "held-out fixture criteria refused"
    # The run really happened, under a per-fixture scratch dir that is now gone.
    assert len(started) == 1
    assert started[0].parent.name.startswith("heldout-scratch-")
    assert started[0].parent.parent == tmp_path / "runs"
    assert not started[0].exists()
    runs = tmp_path / "runs"
    assert not (runs / "golden-runs").exists()
    assert not list(runs.rglob("golden-workspaces"))
    assert not list(runs.glob("heldout-scratch-*"))
    hits = _marker_hits(tmp_path, skip=(root,))
    assert hits == [], hits
    out = tmp_path / "report.json"
    report.write(out)  # type: ignore[attr-defined]
    payload = json.dumps(EvalService().suite_payload(report))
    for text in (out.read_text(encoding="utf-8"), payload):
        assert not any(mark in text for mark in MARKS)


def test_heldout_scratch_is_removed_when_the_run_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from recertia.evals import golden

    class _Boom:
        def start(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("orchestrator failed")

        def close(self) -> None:
            pass

    def _start(runs_root: Path, **_kwargs: object) -> _Boom:
        (runs_root / "runtime.db").parent.mkdir(parents=True, exist_ok=True)
        (runs_root / "runtime.db").write_text(MARK_REQUEST, encoding="utf-8")
        return _Boom()

    monkeypatch.setattr(golden, "_start_orchestrator", _start)
    root = _marked_split(tmp_path)
    with pytest.raises(RuntimeError, match="orchestrator failed"):
        _run(tmp_path, root, _manifest(tmp_path, root))
    assert _marker_hits(tmp_path / "runs", skip=()) == []
    assert not list((tmp_path / "runs").glob("heldout-scratch-*"))


def test_heldout_runs_root_inside_a_checkout_or_the_split_is_refused(tmp_path: Path) -> None:
    root = _split(tmp_path)
    manifest = _manifest(tmp_path, root)
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    repo = Path(__file__).resolve().parents[3]
    assert (repo / ".git").exists()
    cases = {
        checkout / "runs": "outside any git checkout",
        repo / ".recertia" / "heldout-runs-rec009-test": "outside any git checkout",
        root / "runs": "must not overlap",
        tmp_path / "decrypted": "must not overlap",
    }
    for runs_root, message in cases.items():
        with pytest.raises(HeldoutManifestError, match=message) as exc:
            run_heldout_suite(
                manifest_path=manifest,
                heldout_root=root,
                task_class="repo-chore",
                skills_root=_skills(tmp_path),
                runs_root=runs_root,
            )
        assert str(tmp_path) not in str(exc.value) and str(repo) not in str(exc.value)
        assert not runs_root.exists() or runs_root == tmp_path / "decrypted"
    api_root = tmp_path / "api-root"
    with pytest.raises(HeldoutManifestError, match="forbidden root"):
        run_heldout_suite(
            manifest_path=manifest,
            heldout_root=root,
            task_class="repo-chore",
            skills_root=_skills(tmp_path),
            runs_root=api_root / "runs" / "t1",
            forbidden_roots=[api_root],
        )
    store = EvalStore(checkout / "evals.db")
    try:
        with pytest.raises(HeldoutManifestError, match="eval store must be outside"):
            _run(tmp_path, root, manifest, eval_store=store)
    finally:
        store.close()
    assert not (tmp_path / "runs").exists()


def test_fixture_workspace_copy_is_never_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per fixture, independent of the held-out scratch dir: a criteria refusal never
    copies the workspace, and a failure while copying removes the partial copy."""

    from recertia.evals import golden

    root = _marked_split(tmp_path)
    _skills(tmp_path)
    version = SkillStore(tmp_path / "skills").get_version(SKILL, 1)
    runs = tmp_path / "per-fixture-runs"
    kwargs: dict = {
        "runs_root": runs,
        "use_skill_script": True,
        "snapshot_id": None,
        "model_version": None,
        "eval_store": None,
        "heldout": True,
    }
    copies: list[str] = []
    real_copy = shutil.copy2

    def _spy_copy(src: str, dst: str, *args: Any, **kw: Any) -> Any:
        copies.append(Path(dst).name)
        return real_copy(src, dst, *args, **kw)

    monkeypatch.setattr(golden.shutil, "copy2", _spy_copy)
    refused = golden._run_golden_for_skill(version, root / "repo-chore" / "h002", **kwargs)
    assert refused.detail == "held-out fixture criteria refused"
    assert copies == []
    assert _marker_hits(tmp_path, skip=(root,)) == []
    assert not runs.exists() or not any((runs / "golden-workspaces").glob("*"))

    def _failing_copy(src: object, dst: object, *args: object, **kw: object) -> object:
        Path(str(dst)).write_bytes(Path(str(src)).read_bytes())
        raise OSError("copy failed")

    monkeypatch.setattr(golden.shutil, "copy2", _failing_copy)
    with pytest.raises(OSError, match="copy failed"):
        golden._run_golden_for_skill(version, root / "repo-chore" / "h001", **kwargs)
    assert _marker_hits(tmp_path, skip=(root,)) == []
    assert not any((runs / "golden-workspaces").glob("*"))
