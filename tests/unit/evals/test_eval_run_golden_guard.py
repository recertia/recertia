"""REC-009 M4: ``recertia eval run`` refuses golden paths outside a git checkout.

The complement of the held-out loader rule: a decrypted-style split (neutral ``h001``
dirs, outside any checkout) can never run through the public, unverified, unmasked
eval path. Synthetic fixtures only, under ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from recertia.cli.main import app
from recertia.evals.golden import PublicGoldenPathError, require_public_golden_path
from recertia.memory.procedural.store import SkillStore
from tests.support.promotion_fixtures import ABS_PATH, FIXTURE, SKILL, leak_golden, leak_skill

REPO = Path(__file__).resolve().parents[3]


def _args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "eval",
        "run",
        "--task-class",
        "repo-chore",
        "--skills-root",
        str(tmp_path / "skills"),
        "--runs-root",
        str(tmp_path / "runs"),
        "--eval-db",
        str(tmp_path / "evals.db"),
        *extra,
    ]


def _decrypted_split(tmp_path: Path) -> Path:
    root = tmp_path / "decrypted" / "heldout-synthetic-chore-v1"
    leak_golden(
        root / "repo-chore" / "h001",
        command="test -f MUST_EXIST.txt",
        workspace_file=f"{FIXTURE}.txt",
        request=f"synthetic held-out request {FIXTURE}",
    )
    return root


@pytest.mark.parametrize("flag", ["--golden-dir", "--golden-root"])
def test_eval_run_refuses_a_decrypted_split_outside_a_checkout(flag: str, tmp_path: Path) -> None:
    root = _decrypted_split(tmp_path)
    target = root / "repo-chore" / "h001" if flag == "--golden-dir" else root
    result = CliRunner().invoke(app, _args(tmp_path, flag, str(target)))
    assert result.exit_code == 2, result.output
    assert "EVAL REFUSED" in result.stderr and "run_heldout_suite" in result.stderr
    for text in (result.stdout, result.stderr):
        assert FIXTURE not in text and "h001" not in text
        assert str(tmp_path) not in text
        assert not ABS_PATH.search(text), text
    # Nothing ran and nothing was written.
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "evals.db").exists()


def test_require_public_golden_path_is_the_complement_of_the_heldout_rule(tmp_path: Path) -> None:
    require_public_golden_path(REPO / "evals" / "golden")
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    require_public_golden_path(checkout / "evals" / "golden" / "repo-chore" / "h001")
    link = checkout / "evals" / "linked"
    link.parent.mkdir(parents=True)
    link.symlink_to(_decrypted_split(tmp_path), target_is_directory=True)
    for outside in (tmp_path / "decrypted", link):  # realpath: a symlink does not help
        with pytest.raises(PublicGoldenPathError) as exc:
            require_public_golden_path(outside)
        assert str(tmp_path) not in str(exc.value)


def test_eval_run_inside_a_checkout_still_runs_without_echoing_paths(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    golden_root = checkout / "evals" / "golden"
    fixture = leak_golden(golden_root / "repo-chore" / SKILL, command="test -f MUST_EXIST.txt")
    SkillStore(tmp_path / "skills").write_candidate(
        leak_skill(
            1,
            command="python3 -c \"open('MUST_EXIST.txt','w').write('ok')\"",
            criterion_run="test -f MUST_EXIST.txt",
        )
    )
    result = CliRunner().invoke(
        app, _args(tmp_path, "--golden-root", str(golden_root), "--golden-dir", str(fixture))
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["all_passed"] is True and len(payload["results"]) == 1
    assert str(tmp_path) not in result.stdout
    assert not ABS_PATH.search(result.stdout), result.stdout
