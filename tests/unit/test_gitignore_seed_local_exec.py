"""Regression: add-gitignore-entry must work under Windows local-exec (cmd.exe)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from recertia.memory.procedural.seeds import SEED_SKILLS
from recertia.solver import container
from recertia.solver.container import _local_run
from recertia.solver.sandbox import SandboxLimits


def test_add_gitignore_entry_command_appends_pyc_via_local_run(tmp_path: Path) -> None:
    version = next(s for s in SEED_SKILLS if s.skill_id == "add-gitignore-entry")
    command = version.steps[0].inputs["command"]
    assert isinstance(command, str)
    assert "$pattern" not in command

    work = tmp_path / "repo"
    work.mkdir()
    (work / ".gitignore").write_text(".venv/\n", encoding="utf-8")

    proc = _local_run(command, workdir=work, limits=SandboxLimits(), timeout_s=30)
    assert proc.returncode == 0, proc.stderr
    lines = (work / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "*.pyc" in lines
    assert '"$pattern"' not in lines
    assert "$pattern" not in lines

    # Idempotent second run
    proc2 = _local_run(command, workdir=work, limits=SandboxLimits(), timeout_s=30)
    assert proc2.returncode == 0, proc2.stderr
    assert lines.count("*.pyc") == (work / ".gitignore").read_text(encoding="utf-8").splitlines().count(
        "*.pyc"
    )


def test_add_gitignore_cert_command_passes_via_local_run(tmp_path: Path) -> None:
    version = next(s for s in SEED_SKILLS if s.skill_id == "add-gitignore-entry")
    cert = version.certification_criteria[0].run
    assert cert is not None
    work = tmp_path / "repo"
    work.mkdir()
    (work / ".gitignore").write_text(".venv/\n*.pyc\n", encoding="utf-8")
    proc = _local_run(cert, workdir=work, limits=SandboxLimits(), timeout_s=30)
    assert proc.returncode == 0, proc.stderr


def test_local_run_uses_argv_not_shell(tmp_path: Path, monkeypatch) -> None:
    seen: dict[str, object] = {}
    real = subprocess.Popen

    def spy(*args, **kwargs):
        seen["argv"] = args[0]
        seen["shell"] = kwargs.get("shell")
        return real(*args, **kwargs)

    monkeypatch.setattr(container.subprocess, "Popen", spy)
    proc = _local_run("echo hi", workdir=tmp_path, limits=SandboxLimits(), timeout_s=10)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "hi"
    assert seen["shell"] is False
    assert seen["argv"] == ["/bin/sh", "-c", "echo hi"]
