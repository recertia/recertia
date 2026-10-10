"""Hung commands must time out, route to a terminal state, and leave no orphan processes.

Covers the P0: subprocess.TimeoutExpired used to escape validate._run_command and crash
GraphOrchestrator.start, and killing /bin/sh on timeout left its children running.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.budget import Budget
from contracts.criteria import TaskCriterion, mint_rejecting_proof
from contracts.run import Task
from recertia.graph.engine import GraphOrchestrator
from recertia.solver import container
from recertia.solver.sandbox import SandboxLimits

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")


def _marker_seconds() -> int:
    """A sleep duration unlikely to collide with anything else running on the host."""

    return 1000 + uuid.uuid4().int % 8000


def _alive(pattern: str) -> list[int]:
    """PIDs of non-zombie processes whose cmdline contains ``pattern`` (Linux /proc)."""

    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
            state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            continue
        if pattern in cmdline and state != "Z" and int(entry.name) != os.getpid():
            pids.append(int(entry.name))
    return pids


def _assert_no_orphans(pattern: str) -> None:
    if not Path("/proc").is_dir():
        return
    deadline = time.monotonic() + 5
    while _alive(pattern) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _alive(pattern) == [], f"orphaned processes still running {pattern!r}"


@posix_only
def test_hung_validate_command_is_bounded(tmp_path: Path) -> None:
    secs = _marker_seconds()
    base = TaskCriterion(
        id="hangs", kind="command", run=f"sleep {secs}", source="caller", weight=1.0, timeout_s=2
    )
    crit = base.model_copy(update={"sensitivity_proof": mint_rejecting_proof(base, fingerprint="timeout")})
    wd = tmp_path / "ws"
    wd.mkdir()
    orch = GraphOrchestrator(tmp_path / "runs")
    t0 = time.monotonic()
    try:
        state = orch.start(
            "run-hang",
            Task(task_id="t", request="r", submitted_at=datetime.now(timezone.utc)),
            [crit],
            budget=Budget(max_attempts=1),
            workdir=wd,
            script=["true"],
        )
    finally:
        orch.close()
    elapsed = time.monotonic() - t0
    assert elapsed < 30, f"timeout not enforced: {elapsed:.1f}s"
    assert state.terminal is not None and state.terminal != "solved"
    assert state.results and not any(r.passed for r in state.results)
    result = next(r for r in state.results if r.criterion_id == "hangs")
    assert result.exit_code == 124
    _assert_no_orphans(f"sleep {secs}")


@posix_only
@pytest.mark.parametrize("shape", ["simple", "compound", "background_child"])
def test_local_run_kills_whole_process_tree_on_timeout(tmp_path: Path, shape: str) -> None:
    secs = _marker_seconds()
    command = {
        "simple": f"sleep {secs}",
        "compound": f"sleep {secs}; true",
        "background_child": f"sleep {secs} & sleep {secs + 1}; wait",
    }[shape]
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        container._local_run(command, workdir=tmp_path, limits=SandboxLimits(), timeout_s=1)
    assert time.monotonic() - t0 < 10
    _assert_no_orphans(f"sleep {secs}")
    _assert_no_orphans(f"sleep {secs + 1}")


def test_local_run_still_returns_output_and_exit_code(tmp_path: Path) -> None:
    proc = container._local_run(
        "echo out; echo err >&2; exit 3", workdir=tmp_path, limits=SandboxLimits(), timeout_s=10
    )
    assert proc.returncode == 3
    assert proc.stdout.strip() == "out"
    assert proc.stderr.strip() == "err"


def test_container_timeout_kills_named_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No Docker needed: the runtime CLI is faked, the kill-by-name contract is asserted."""

    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if args[1] == "run":
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))  # type: ignore[arg-type]
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(container.subprocess, "run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired):
        container._container_run(
            "docker", "sleep 999", workdir=tmp_path, spec=container.ContainerSpec(), timeout_s=1
        )
    run_args, kill_args = calls
    names = [a.split("=", 1)[1] for a in run_args if a.startswith("--name=")]
    assert len(names) == 1 and names[0].startswith("rec-")
    assert kill_args == ["docker", "kill", names[0]]


def test_container_names_are_unique(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.extend(a for a in args if a.startswith("--name="))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(container.subprocess, "run", fake_run)
    for _ in range(3):
        container._container_run(
            "docker", "true", workdir=tmp_path, spec=container.ContainerSpec(), timeout_s=5
        )
    assert len(set(seen)) == 3


def _docker_ready() -> bool:
    if container.container_runtime() != "docker":
        return False
    try:
        out = subprocess.run(
            ["docker", "image", "inspect", "python:3.12-slim"], capture_output=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


@pytest.mark.skipif(not _docker_ready(), reason="needs docker with python:3.12-slim pulled")
def test_real_container_is_gone_after_timeout(tmp_path: Path) -> None:
    secs = _marker_seconds()
    with pytest.raises(subprocess.TimeoutExpired):
        container._container_run(
            "docker", f"sleep {secs}", workdir=tmp_path, spec=container.ContainerSpec(), timeout_s=3
        )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        ps = subprocess.run(
            ["docker", "ps", "-q", "--filter", "name=^rec-"], capture_output=True, text=True, timeout=20
        )
        running = [
            cid
            for cid in ps.stdout.split()
            if f"sleep {secs}"
            in subprocess.run(
                ["docker", "inspect", "-f", "{{.Args}}", cid], capture_output=True, text=True, timeout=20
            ).stdout
        ]
        if not running:
            return
        time.sleep(0.5)
    pytest.fail(f"container still running after timeout: {running}")


def test_solve_script_maps_timeout_to_exit_124(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from recertia.nodes import solve_script

    def boom(command: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, 60)

    monkeypatch.setattr(container, "run_configured_command", boom)
    out = solve_script.run_container_command("sleep 999", tmp_path)
    assert out["returncode"] == 124
    assert "timed out" in out["stderr"]
