"""REC-020: the commit-email check covers the pushed range on push events.

Each test builds a throwaway git repo in ``tmp_path`` and runs
``scripts/check_commit_trailers.py`` the way CI does (env vars only).
Emails are synthetic placeholders assembled at runtime.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_commit_trailers.py"
ZERO = "0" * 40
GOOD = "1+placeholder" + "@users.noreply." + "github.com"
BAD = "x" + "@" + "example.invalid"


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("GIT_")
        and k not in {"BASE", "HEAD", "PUSH_BEFORE", "PUSH_AFTER", "PUSH_REF", "DEFAULT_BRANCH"}
    }
    env.update(HOME=str(tmp_path), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    env.update(extra)
    return env


class Repo:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.path = tmp_path / "repo"
        self.path.mkdir()
        self.git("init", "-q", "-b", "main")
        self.n = 0

    def git(self, *args: str, email: str = GOOD) -> str:
        env = _env(
            self.tmp,
            GIT_AUTHOR_NAME="placeholder",
            GIT_AUTHOR_EMAIL=email,
            GIT_COMMITTER_NAME="placeholder",
            GIT_COMMITTER_EMAIL=email,
        )
        return subprocess.check_output(["git", *args], cwd=self.path, env=env, text=True).strip()

    def commit(self, email: str = GOOD, coauthor: str | None = None) -> str:
        self.n += 1
        (self.path / f"f{self.n}.txt").write_text(str(self.n))
        self.git("add", ".")
        msg = f"change {self.n}"
        if coauthor:
            msg += f"\n\nCo-authored-by: placeholder <{coauthor}>"
        self.git("commit", "-q", "-m", msg, email=email)
        return self.git("rev-parse", "HEAD")

    def publish_main(self) -> None:
        """Stand in for actions/checkout: origin/main points at the pushed tip."""
        self.git("update-ref", "refs/remotes/origin/main", "main")

    def check(self, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=self.path,
            env=_env(self.tmp, **env),
            capture_output=True,
            text=True,
        )


def _push(repo: Repo, before: str, after: str, ref: str = "main") -> subprocess.CompletedProcess[str]:
    return repo.check(PUSH_BEFORE=before, PUSH_AFTER=after, PUSH_REF=ref, DEFAULT_BRANCH="main")


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


# --- direct push to main: before..after -------------------------------------


def test_direct_push_to_main_with_bad_commit_fails(repo: Repo) -> None:
    before = repo.commit()
    repo.commit()
    bad = repo.commit(email=BAD)
    repo.publish_main()
    # The old push path (merge-base origin/main HEAD) is HEAD itself: empty.
    assert repo.git("merge-base", "origin/main", "HEAD") == bad
    result = _push(repo, before, bad)
    assert result.returncode == 1, result
    assert f"FAIL {bad}" in result.stderr
    assert "1 commit(s)" in result.stderr


def test_direct_push_to_main_with_bad_coauthor_fails(repo: Repo) -> None:
    before = repo.commit()
    after = repo.commit(coauthor=BAD)
    repo.publish_main()
    result = _push(repo, before, after)
    assert result.returncode == 1, result
    assert "Co-authored-by=" in result.stderr


def test_direct_push_to_main_with_good_commits_passes(repo: Repo) -> None:
    repo.commit(email=BAD)  # already on main before the push: out of range
    before = repo.commit()
    repo.commit()
    after = repo.commit(coauthor=GOOD)
    repo.publish_main()
    result = _push(repo, before, after)
    assert result.returncode == 0, result
    assert f"ok 2 commit(s) in {before}..{after}" in result.stdout


# --- before is all zeros ------------------------------------------------------


def test_zero_before_new_branch_checks_commits_since_default_branch(repo: Repo) -> None:
    repo.commit(email=BAD)  # historical, on main
    base = repo.commit()
    repo.publish_main()
    repo.git("checkout", "-q", "-b", "feature")
    repo.commit()
    after = repo.commit()
    result = _push(repo, ZERO, after, ref="feature")
    assert result.returncode == 0, result
    assert f"ok 2 commit(s) in {base}..{after}" in result.stdout

    bad = repo.commit(email=BAD)
    result = _push(repo, ZERO, bad, ref="feature")
    assert result.returncode == 1, result
    assert f"FAIL {bad}" in result.stderr


def test_zero_before_on_default_branch_checks_after_alone(repo: Repo) -> None:
    # Pre-policy history on the default branch is not walked or grandfathered.
    repo.commit(email=BAD)
    repo.commit(coauthor=BAD)
    after = repo.commit()
    repo.publish_main()
    result = _push(repo, ZERO, after)
    assert result.returncode == 0, result
    assert f"ok 1 commit(s) in --max-count=1 {after}" in result.stdout

    bad = repo.commit(email=BAD)
    repo.publish_main()
    result = _push(repo, ZERO, bad)
    assert result.returncode == 1, result
    assert f"FAIL {bad}" in result.stderr
    assert "1 commit(s)" in result.stderr


def test_zero_before_unrelated_branch_checks_after_alone(repo: Repo) -> None:
    repo.commit()
    repo.publish_main()
    repo.git("checkout", "-q", "--orphan", "other")
    repo.commit(email=BAD)
    after = repo.commit()
    result = _push(repo, ZERO, after, ref="other")
    assert result.returncode == 0, result
    assert "ok 1 commit(s)" in result.stdout


def test_zero_before_new_branch_at_existing_commit_checks_after_alone(repo: Repo) -> None:
    repo.commit()
    after = repo.commit()
    repo.publish_main()
    result = _push(repo, ZERO, after, ref="feature")
    assert result.returncode == 0, result
    assert "ok 1 commit(s)" in result.stdout


def test_unknown_before_uses_the_same_fallback(repo: Repo) -> None:
    bad = repo.commit(email=BAD)
    repo.publish_main()
    result = _push(repo, "f" * 40, bad)
    assert result.returncode == 1, result
    assert f"FAIL {bad}" in result.stderr


# --- allow-list forms ---------------------------------------------------------

AGENT = "cursoragent@" + "cursor.com"
WEB_FLOW = "noreply@" + "github.com"


def test_agent_author_and_coauthor_forms_pass(repo: Repo) -> None:
    before = repo.commit()
    repo.commit(email=AGENT, coauthor=AGENT)
    after = repo.commit(email=GOOD, coauthor=GOOD)
    result = _push(repo, before, after)
    assert result.returncode == 0, result
    assert "ok 2 commit(s)" in result.stdout


def test_web_flow_is_allowed_as_committer_only(repo: Repo) -> None:
    before = repo.commit()
    (repo.path / "m.txt").write_text("m")
    repo.git("add", ".")
    env = _env(
        repo.tmp,
        GIT_AUTHOR_NAME="placeholder",
        GIT_AUTHOR_EMAIL=GOOD,
        GIT_COMMITTER_NAME="placeholder",
        GIT_COMMITTER_EMAIL=WEB_FLOW,
    )
    subprocess.check_call(["git", "commit", "-q", "-m", "squash"], cwd=repo.path, env=env)
    merged = repo.git("rev-parse", "HEAD")
    result = _push(repo, before, merged)
    assert result.returncode == 0, result

    as_author = repo.commit(email=WEB_FLOW)
    result = _push(repo, merged, as_author)
    assert result.returncode == 1, result
    as_coauthor = repo.commit(coauthor=WEB_FLOW)
    result = _push(repo, as_author, as_coauthor)
    assert result.returncode == 1, result
    assert "Co-authored-by=" in result.stderr


# --- pull_request path is unchanged ------------------------------------------


def test_pull_request_range_ignores_push_inputs(repo: Repo) -> None:
    repo.commit(email=BAD)
    base = repo.commit()
    head = repo.commit()
    result = repo.check(BASE=base, HEAD=head, PUSH_BEFORE=ZERO, PUSH_AFTER=head)
    assert result.returncode == 0, result
    assert f"ok 1 commit(s) in {base}..{head}" in result.stdout

    bad = repo.commit(email=BAD)
    result = repo.check(BASE=base, HEAD=bad)
    assert result.returncode == 1, result
    assert f"FAIL {bad}" in result.stderr


def test_missing_inputs_exit_2(repo: Repo) -> None:
    repo.commit()
    assert repo.check().returncode == 2
