"""SEC-11: tip sha, production docs, and API umask."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recertia.api import create_app
from recertia.programs.git_tip import GitTipError, assert_tip_sha, checkout_tip


def test_tip_sha_rejects_options_and_refs() -> None:
    with pytest.raises(GitTipError, match="40 lowercase"):
        assert_tip_sha("--force")
    with pytest.raises(GitTipError, match="40 lowercase"):
        assert_tip_sha("HEAD")
    assert assert_tip_sha("a" * 40) == "a" * 40


def test_checkout_uses_end_of_options(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "README.md").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dest = tmp_path / "out"
    checked = checkout_tip(binding_root=repo, tip_sha=sha, dest=dest)
    assert checked == sha
    with pytest.raises(GitTipError, match="40 lowercase"):
        checkout_tip(binding_root=repo, tip_sha="--force", dest=tmp_path / "bad")


def test_prod_unmounts_interactive_docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_ENV", "prod")
    app = create_app(root=tmp_path / "api-root")
    client = TestClient(app)
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_api_startup_sets_umask_077(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECERTIA_ENV", raising=False)
    previous = os.umask(0o022)
    os.umask(previous)
    create_app(root=tmp_path / "api-root")
    probe = tmp_path / "created-after-startup"
    fd = os.open(probe, os.O_CREAT | os.O_WRONLY, 0o666)
    os.close(fd)
    assert probe.stat().st_mode & 0o077 == 0
