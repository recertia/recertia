"""REC-022: weekly-ops cannot be pointed at a non-public eval root (evals/heldout or any private path)."""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github/workflows/weekly-ops.yml"
PUBLIC = {
    "EVAL_GOLDEN_ROOT": "evals/golden",
    "EVAL_PROBES": "evals/probes/repo-chore.json",
    "EVAL_CANARY_ROOT": "evals/canary/planted-failure",
}


def _checker():
    path = REPO / "scripts/check_public_eval_root.py"
    spec = importlib.util.spec_from_file_location("check_public_eval_root", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "evals/golden/repo-chore/t1").mkdir(parents=True)
    (repo / "evals/golden/repo-chore/t1/task.json").write_text("{}\n")
    (repo / "evals/probes").mkdir(parents=True)
    (repo / "evals/probes/p.json").write_text("[]\n")
    (repo / "evals/heldout").mkdir(parents=True)
    (repo / "evals/heldout/m.json").write_text("{}\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    return repo


def test_real_public_roots_pass() -> None:
    assert _checker().check(list(PUBLIC.values()), {}) == []


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("evals/heldout", "private_root"),
        ("evals/heldout/m.json", "private_root"),
        ("evals/golden/heldout-copy", "private_root"),
        ("/tmp/private", "absolute_path"),
        ("evals/golden/../heldout", "parent_traversal"),
        ("../outside/evals/golden", "parent_traversal"),
        ("evals", "not_public_root"),
        ("skills", "not_public_root"),
        ("evals/golden-private", "not_public_root"),
        ("evals/golden/nope", "missing"),
        ("", "malformed"),
    ],
)
def test_non_public_paths_are_refused(fake_repo: Path, raw: str, reason: str) -> None:
    assert _checker().check_path(raw, fake_repo) == reason


def test_fake_repo_public_paths_pass(fake_repo: Path) -> None:
    mod = _checker()
    assert mod.check_path("evals/golden", fake_repo) is None
    assert mod.check_path("evals/probes/p.json", fake_repo) is None


def test_symlink_out_of_public_root_is_refused(fake_repo: Path) -> None:
    (fake_repo / "evals/golden/repo-chore/link").symlink_to(fake_repo / "evals/heldout")
    assert _checker().check_path("evals/golden", fake_repo) == "escapes_public_root"


def test_public_root_symlinked_elsewhere_is_refused(fake_repo: Path, tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir()
    (private / "x.json").write_text("{}\n")
    (fake_repo / "evals/canary").mkdir()
    (fake_repo / "evals/canary/c").symlink_to(private)
    assert _checker().check_path("evals/canary/c", fake_repo) == "escapes_public_root"


def test_untracked_file_in_public_root_is_refused(fake_repo: Path) -> None:
    (fake_repo / "evals/golden/repo-chore/t2").mkdir()
    (fake_repo / "evals/golden/repo-chore/t2/task.json").write_text("{}\n")
    assert _checker().check_path("evals/golden", fake_repo) == "untracked"


def test_modified_tracked_file_in_public_root_is_refused(fake_repo: Path) -> None:
    """CTO-rec N7: a decrypted file written over a tracked path is caught, not only new files."""

    mod = _checker()
    target = fake_repo / "evals/golden/repo-chore/t1/task.json"
    target.write_text('{"private": true}\n')
    assert mod.check_path("evals/golden", fake_repo) == "modified"
    assert mod.check_path("evals/probes/p.json", fake_repo) is None
    target.write_text("{}\n")
    assert mod.check_path("evals/golden", fake_repo) is None


def test_heldout_env_and_no_paths_are_refused() -> None:
    errors = _checker().check(list(PUBLIC.values()), {"HELDOUT_MANIFEST": "x"})
    assert errors == ["heldout_env_set count=1"]
    assert _checker().check([], {}) == ["no_paths"]


def test_cli_output_never_echoes_paths(capsys: pytest.CaptureFixture[str]) -> None:
    assert _checker().main(["/tmp/secret-name", "evals/heldout"]) == 1
    out = capsys.readouterr().out
    assert "secret-name" not in out and "heldout" not in out.replace("heldout_env", "")
    assert "arg=0 reason=absolute_path" in out and "arg=1 reason=private_root" in out


def test_workflow_hard_codes_public_roots_and_validates_first() -> None:
    text = WORKFLOW.read_text()
    # No inputs: workflow_dispatch / schedule only, nothing operator-settable.
    assert "inputs:" not in text and "inputs." not in text
    for key, value in PUBLIC.items():
        assert re.search(rf"^\s+{key}: {re.escape(value)}$", text, re.M), key
    assert re.search(r"^\s+HELDOUT_", text, re.M) is None
    validate = text.index(
        'scripts/check_public_eval_root.py "$EVAL_GOLDEN_ROOT" "$EVAL_PROBES" "$EVAL_CANARY_ROOT"'
    )
    for use in (
        '--probes "$EVAL_PROBES"',
        '--golden-root "$EVAL_GOLDEN_ROOT"',
        '--canary-root "$EVAL_CANARY_ROOT"',
    ):
        assert validate < text.index(use), use
    # Every eval-root flag goes through a validated constant; no literal or other root.
    # (``soak record --probes artifacts/probes.json`` reads this job's own output, not an eval root.)
    for flag in ("--golden-root", "--probes", "--canary-root"):
        for m in re.finditer(rf"{flag}\s+(\S+)", text):
            assert m.group(1).startswith('"$EVAL_') or m.group(1).startswith("artifacts/"), (flag, m.group(1))
    assert len(re.findall(r'--golden-root "\$EVAL_GOLDEN_ROOT"', text)) == text.count("--golden-root")
    for cmd in ("recertia probes run", "recertia eval run"):
        assert cmd in text
    # Live canary must not run without an explicit root either.
    for m in re.finditer(r"recertia canary\b[^\n]*", text):
        assert "--canary-root" in m.group(0)
