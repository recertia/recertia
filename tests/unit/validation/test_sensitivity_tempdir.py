"""REC-046: author_sensitivity_proof leaves no recertia-neg-* directory behind."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from contracts.criteria import SkillCertificationCriterion, TaskCriterion
from recertia.validation.sensitivity import author_sensitivity_proof

CRITERION = TaskCriterion(id="c1", kind="command", run="test -f output.txt", source="caller")


def _neg_dirs(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.name.startswith("recertia-neg-"))


@pytest.fixture
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point tempfile at an empty directory so leftovers can be listed."""

    root = tmp_path / "tempfile-root"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def test_default_negative_fixture_removed_after_success(temp_root: Path) -> None:
    seen: list[Path] = []

    def runner(criterion: TaskCriterion | SkillCertificationCriterion, workdir: Path) -> bool:
        assert workdir.is_dir()
        assert workdir.parent == temp_root
        assert workdir.name.startswith("recertia-neg-")
        (workdir / "sub").mkdir()
        (workdir / "sub" / "written-by-criterion.txt").write_text("x")
        seen.append(workdir)
        return False

    proof = author_sensitivity_proof(CRITERION, runner=runner)

    assert proof.rejected is True
    assert proof.criterion_id == "c1"
    assert len(seen) == 1
    assert not seen[0].exists()
    assert _neg_dirs(temp_root) == []


def test_default_negative_fixture_removed_when_runner_raises(temp_root: Path) -> None:
    seen: list[Path] = []

    def runner(criterion: TaskCriterion | SkillCertificationCriterion, workdir: Path) -> bool:
        assert workdir.is_dir()
        assert workdir.name.startswith("recertia-neg-")
        (workdir / "partial.txt").write_text("x")
        seen.append(workdir)
        raise RuntimeError("runner failed mid-call")

    with pytest.raises(RuntimeError, match="runner failed mid-call"):
        author_sensitivity_proof(CRITERION, runner=runner)

    assert len(seen) == 1
    assert not seen[0].exists()
    assert _neg_dirs(temp_root) == []


def test_default_runner_rejects_in_removed_fixture(temp_root: Path) -> None:
    proof = author_sensitivity_proof(CRITERION)

    assert proof.rejected is True
    assert _neg_dirs(temp_root) == []


def test_caller_supplied_negative_workdir_is_kept(tmp_path: Path) -> None:
    neg = tmp_path / "neg"
    neg.mkdir()

    author_sensitivity_proof(CRITERION, negative_workdir=neg, runner=lambda c, w: False)
    author_sensitivity_proof(CRITERION, negative_workdir=neg, runner=lambda c, w: False)

    assert neg.is_dir()
