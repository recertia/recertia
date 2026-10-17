"""GP2 git_tip handoff: registered bindings, tip record, fresh-workdir checkout."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from contracts.program import MigrationProgram, MigrationStep, RepoBinding

_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


class GitTipError(ValueError):
    """Invalid binding, tip, or checkout."""


def assert_tip_sha(sha: str) -> str:
    """Accept only a 40-character lowercase commit id. Options never reach git."""

    if _COMMIT_SHA.fullmatch(sha) is None:
        raise GitTipError("tip_sha must be 40 lowercase hex characters")
    return sha


def tenant_bindings_root(api_root: Path, tenant_id: str) -> Path:
    return (api_root / "repo_bindings" / tenant_id).resolve()


def resolve_binding_root(api_root: Path, tenant_id: str, binding: RepoBinding) -> Path:
    """Resolve and validate an allowlisted binding root."""

    base = tenant_bindings_root(api_root, tenant_id)
    base.mkdir(parents=True, exist_ok=True)
    rel = binding.root.strip().lstrip("/")
    if not rel or ".." in Path(rel).parts or Path(rel).is_absolute():
        raise GitTipError("repo_binding.root must be a relative path without '..'")
    root = (base / rel).resolve()
    try:
        root.relative_to(base)
    except ValueError as exc:
        raise GitTipError("repo_binding.root escapes tenant repo_bindings/") from exc
    if not root.is_dir():
        raise GitTipError(f"repo_binding root does not exist: {rel}")
    if not (root / ".git").exists():
        raise GitTipError(f"repo_binding root is not a git repository: {rel}")
    return root


def assert_git_tip_program(program: MigrationProgram) -> None:
    if program.handoff != "git_tip":
        return
    if program.repo_binding is None:
        raise GitTipError("handoff=git_tip requires a registered repo_binding")


def git_rev_parse(repo: Path, ref: str = "HEAD") -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--end-of-options", ref],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise GitTipError(f"git rev-parse failed for {ref}: {exc}") from exc
    sha = out.stdout.strip()
    if len(sha) < 7:
        raise GitTipError(f"invalid git sha from rev-parse: {sha!r}")
    return sha


def record_tip(repo: Path) -> str:
    """Return HEAD sha for a git worktree."""

    return git_rev_parse(repo, "HEAD")


def predecessor_tip(program: MigrationProgram, step: MigrationStep) -> str | None:
    """Tip from the previous succeeded step, if recorded."""

    prior = [s for s in program.steps if s.ordinal == step.ordinal - 1]
    if not prior:
        return None
    eh = prior[0].external_handoff
    if eh and eh.head_sha:
        return eh.head_sha
    return None


def resolve_tip_sha(
    program: MigrationProgram,
    step: MigrationStep,
    *,
    api_root: Path,
    explicit: str | None = None,
) -> str:
    """Resolve tip for seeding: explicit > predecessor > binding default branch."""

    assert_git_tip_program(program)
    assert program.repo_binding is not None
    if explicit:
        return assert_tip_sha(explicit)
    pred = predecessor_tip(program, step)
    if pred:
        return pred
    root = resolve_binding_root(api_root, program.tenant_id, program.repo_binding)
    branch = program.repo_binding.default_branch
    return git_rev_parse(root, branch)


def checkout_tip(*, binding_root: Path, tip_sha: str, dest: Path) -> str:
    """Clone binding into dest and check out tip_sha (fresh workdir; no shared mount).

    Returns the checked-out HEAD sha.
    """

    sha = assert_tip_sha(tip_sha)
    if dest.exists() and any(dest.iterdir()):
        raise GitTipError(f"seed destination is not empty: {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "clone", "--local", "--no-checkout", str(binding_root), str(dest)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        subprocess.run(
            ["git", "-C", str(dest), "switch", "--detach", "--end-of-options", sha],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        # Leave no partial alias: wipe dest on failure
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        raise GitTipError(f"git checkout tip failed: {exc}") from exc
    return git_rev_parse(dest, "HEAD")
