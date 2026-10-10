"""Execution kill switches must stay latched (GROVE test_kills style)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from recertia.solver import container, sandbox
from recertia.solver.sandbox import SandboxError, SandboxLimits


def test_host_subprocess_sandbox_is_dead(tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="host subprocess sandbox is disabled"):
        sandbox.run_sandboxed("true", workdir=tmp_path)


def test_container_refuses_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[object] = []
    monkeypatch.setattr(container, "_container_run", lambda *a, **k: called.append(a))
    with pytest.raises(SandboxError, match="refuses allow_network=True"):
        container.run_in_container(
            "true", workdir=tmp_path, limits=SandboxLimits(allow_network=True)
        )
    assert not called, "network refusal must fire before any runtime call"


def test_sandbox_limits_default_no_network() -> None:
    assert SandboxLimits().allow_network is False
    assert SandboxLimits.from_policy(None).allow_network is False


def test_container_spec_default_network_none() -> None:
    field = {f.name: f for f in dataclasses.fields(container.ContainerSpec)}["network"]
    assert field.default == "none"
    assert container.ContainerSpec(image="python:3.12-slim").network == "none"
