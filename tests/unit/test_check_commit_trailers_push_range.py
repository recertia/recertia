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

    def commit(self, email: str = GOOD, coauthor: str | None = None, trailers: tuple[str, ...] = ()) -> str:
        self.n += 1
        (self.path / f"f{self.n}.txt").write_text(str(self.n))
        self.git("add", ".")
        lines = list(trailers)
        if coauthor:
            lines.append(f"Co-authored-by: placeholder <{coauthor}>")
        msg = f"change {self.n}"
        if lines:
            msg += "\n\n" + "\n".join(lines)
        self.git("commit", "-q", "-m", msg, email=email)
        return self.git("rev-parse", "HEAD")

    def raw_commit(self, author_email: str) -> str:
        """A commit object with an arbitrary (possibly empty) author email."""
        tree = self.git("write-tree")
        parent = self.git("rev-parse", "HEAD")
        body = (
            f"tree {tree}\nparent {parent}\n"
            f"author placeholder <{author_email}> 1700000000 +0000\n"
            f"committer placeholder <{GOOD}> 1700000000 +0000\n\nraw\n"
        )
        sha = subprocess.run(
            ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
            cwd=self.path,
            env=_env(self.tmp),
            input=body,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.git("update-ref", "HEAD", sha)
        return sha

    def drop_unreachable(self) -> None:
        """Remove objects no ref reaches, like an old tip a fresh clone never had."""
        self.git("reflog", "expire", "--expire=now", "--all")
        self.git("gc", "-q", "--prune=now")

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


def test_zero_before_on_default_branch_fails_closed(repo: Repo) -> None:
    # Creating the default branch: history is not grandfathered and the tip
    # alone is not enough, so the check fails closed even if the tip is good.
    repo.commit(email=BAD)
    after = repo.commit()
    repo.publish_main()
    for before in (ZERO, ""):
        result = _push(repo, before, after)
        assert result.returncode == 1, result
        assert "creates the default branch" in result.stderr
        assert "Failing closed" in result.stderr


def test_zero_before_unrelated_branch_fails_closed(repo: Repo) -> None:
    repo.commit()
    repo.publish_main()
    repo.git("checkout", "-q", "--orphan", "other")
    repo.commit(email=BAD)
    after = repo.commit()
    result = _push(repo, ZERO, after, ref="other")
    assert result.returncode == 1, result
    assert "shares no history" in result.stderr


def test_zero_before_without_default_branch_ref_fails_closed(repo: Repo) -> None:
    after = repo.commit()
    result = _push(repo, ZERO, after, ref="feature")
    assert result.returncode == 1, result
    assert "origin/main is not in this clone" in result.stderr


def test_zero_before_new_branch_at_existing_commit_checks_after_alone(repo: Repo) -> None:
    repo.commit()
    after = repo.commit()
    repo.publish_main()
    result = _push(repo, ZERO, after, ref="feature")
    assert result.returncode == 0, result
    assert f"ok 1 commit(s) in commit {after[:12]} (tip only, empty push range)" in result.stdout


# --- force push (CTO case e5) -------------------------------------------------


def _force_push_setup(repo: Repo) -> tuple[str, str, str]:
    """main had old_tip; it is rewritten to base -> bad -> good (new tip)."""
    base = repo.commit()
    old_tip = repo.commit()
    repo.git("reset", "-q", "--hard", base)
    bad = repo.commit(email=BAD)
    after = repo.commit()  # the tip itself is compliant
    repo.publish_main()
    return old_tip, bad, after


def test_e5_force_push_to_main_with_missing_old_tip_fails_closed(repo: Repo) -> None:
    old_tip, bad, after = _force_push_setup(repo)
    repo.drop_unreachable()
    assert (
        subprocess.run(
            ["git", "cat-file", "-e", f"{old_tip}^{{commit}}"], cwd=repo.path, capture_output=True
        ).returncode
        != 0
    )
    result = _push(repo, old_tip, after)
    assert result.returncode == 1, result
    assert "not in this clone" in result.stderr
    assert "Failing closed" in result.stderr
    assert "ok " not in result.stdout


def test_e5_force_push_to_main_with_fetched_old_tip_checks_rewritten_range(repo: Repo) -> None:
    # CI fetches PUSH_BEFORE first; with the old tip present, before..after
    # includes the earlier bad commit, not just the tip.
    old_tip, bad, after = _force_push_setup(repo)
    result = _push(repo, old_tip, after)
    assert result.returncode == 1, result
    assert f"FAIL {bad}" in result.stderr


def test_force_push_to_main_good_rewrite_passes(repo: Repo) -> None:
    base = repo.commit()
    old_tip = repo.commit()
    repo.git("reset", "-q", "--hard", base)
    repo.commit()
    after = repo.commit(email=AGENT)
    repo.publish_main()
    result = _push(repo, old_tip, after)
    assert result.returncode == 0, result
    assert f"ok 2 commit(s) in {old_tip}..{after}" in result.stdout


def test_missing_before_on_feature_branch_uses_merge_base(repo: Repo) -> None:
    base = repo.commit()
    repo.publish_main()
    repo.git("checkout", "-q", "-b", "feature")
    bad = repo.commit(email=BAD)
    after = repo.commit()
    result = _push(repo, "f" * 40, after, ref="feature")
    assert result.returncode == 1, result
    assert f"FAIL {bad}" in result.stderr
    assert f"in {base}..{after}" in result.stderr


# --- allow-list forms ---------------------------------------------------------

AGENT = "cursoragent@" + "cursor.com"
WEB_FLOW = "noreply@" + "github.com"
IDENTITY_TRAILERS = (
    "Co-authored-by",
    "Signed-off-by",
    "Reviewed-by",
    "Acked-by",
    "Tested-by",
    "Reported-by",
    "sIGNED-oFF-bY",
    "REVIEWED-BY",
    "co-AUTHORED-by",
)


@pytest.mark.parametrize("key", IDENTITY_TRAILERS)
def test_every_by_trailer_is_scanned_case_insensitively(repo: Repo, key: str) -> None:
    before = repo.commit()
    after = repo.commit(trailers=(f"{key}: placeholder <{BAD}>",))
    result = _push(repo, before, after)
    assert result.returncode == 1, result
    assert f"{key}={BAD}" in result.stderr


def test_good_by_trailers_pass(repo: Repo) -> None:
    before = repo.commit()
    after = repo.commit(
        trailers=tuple(f"{key}: placeholder <{GOOD}>" for key in IDENTITY_TRAILERS)
        + (f"Signed-off-by: placeholder <{AGENT}>", "Refs: #97")
    )
    result = _push(repo, before, after)
    assert result.returncode == 0, result


def test_signed_off_by_without_email_fails(repo: Repo) -> None:
    before = repo.commit()
    after = repo.commit(trailers=("Signed-off-by: placeholder",))
    result = _push(repo, before, after)
    assert result.returncode == 1, result
    assert "Signed-off-by=placeholder" in result.stderr


def test_empty_author_email_fails(repo: Repo) -> None:
    before = repo.commit()
    after = repo.raw_commit("")
    assert repo.git("log", "-1", "--format=%ae", after) == ""
    result = _push(repo, before, after)
    assert result.returncode == 1, result
    assert f"FAIL {after}: author=<empty>" in result.stderr


def test_empty_trailer_email_fails(repo: Repo) -> None:
    before = repo.commit()
    after = repo.commit(trailers=("Signed-off-by: placeholder <>",))
    result = _push(repo, before, after)
    assert result.returncode == 1, result
    assert "Signed-off-by=<empty>" in result.stderr


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
