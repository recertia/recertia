"""HOME is the workdir unless single-node passthrough is set."""

from __future__ import annotations

from pathlib import Path

from recertia.ports.execution import CommandRequest, ExecutionConfig, LocalExecutionCapability
from recertia.solver.executor import LocalExecutor


def _config(**overrides: object) -> ExecutionConfig:
    base = dict(
        backend="local",
        runtime=None,
        image="python:3.12-slim",
        allow_custom_image=False,
        workdir_world_write=False,
        api_allow_local_exec=True,
        local_home_passthrough=False,
    )
    base.update(overrides)
    return ExecutionConfig(**base)  # type: ignore[arg-type]


def test_home_is_workdir_even_when_host_home_is_set(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", "/tmp/host-home-should-not-leak")
    executor = LocalExecutor(_config(), LocalExecutionCapability(purpose="pytest"))
    result = executor.run(
        CommandRequest(
            command='python3 -c \'import os; print(os.environ["HOME"])\'',
            workdir=tmp_path,
            timeout_s=15,
        )
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(tmp_path.resolve())


def test_home_passthrough_is_single_node_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", "/tmp/host-home-passthrough")
    single = LocalExecutor(
        _config(local_home_passthrough=True, deployment_mode="single_node"),
        LocalExecutionCapability(purpose="pytest"),
    )
    passed = single.run(
        CommandRequest(
            command='python3 -c \'import os; print(os.environ["HOME"])\'',
            workdir=tmp_path,
            timeout_s=15,
        )
    )
    assert passed.stdout.strip() == "/tmp/host-home-passthrough"
    multi = LocalExecutor(
        _config(local_home_passthrough=True, deployment_mode="multi_node"),
        LocalExecutionCapability(purpose="pytest"),
    )
    refused = multi.run(
        CommandRequest(
            command='python3 -c \'import os; print(os.environ["HOME"])\'',
            workdir=tmp_path,
            timeout_s=15,
        )
    )
    assert refused.stdout.strip() == str(tmp_path.resolve())
